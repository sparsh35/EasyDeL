
# Copyright 2023 The EASYDEL Author @erfanzar (Erfan Zare Chavoshi).
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import functools
import math
from typing import Optional, Tuple, Union

import chex
import flax.linen
import flax.linen.partitioning
import jax
from flax import linen as nn
from flax.core import FrozenDict, freeze, unfreeze
from flax.linen import Dense, combine_masks
from flax.traverse_util import flatten_dict, unflatten_dict
from jax import lax
from jax import numpy as jnp
from jax.sharding import PartitionSpec

from easydel.modules.attention_module import FlexibleAttentionModule
from easydel.modules.falcon.falcon_configuration import FalconConfig as FalconConfig
from easydel.modules.falcon.kernels import falcon_mlp_pallas
from easydel.modules.flax_modeling_utils import (
	FlaxAttentionModule,
	apply_rotary_pos_emb,
	block_wise_ffn,
	control_mlp_sharding,
	get_dot_general_by_bits,
	get_gradient_checkpoint_policy,
	precompute_frequencies,
	with_sharding_constraint,
)
from easydel.modules.modeling_flax_outputs import (
	FlaxBaseModelOutput,
	FlaxCausalLMOutput,
)
from easydel.modules.modeling_utils import EDPretrainedModel


def built_bloom_alibi(attention_mask, num_attention_heads):
	"""The built_bloom_alibi function is used to create a bloom alibi for the attention mask.
	The bloom alibi is used in the Bloom Attention layer to ensure that each token has a unique
	attention vector, even if it's masked out. This ensures that all tokens have an equal chance of being selected as
	the most important token in the sequence, which helps with training stability and performance.

	Args:
	    attention_mask: Mask out the padding tokens in the input
	        sequence
	    num_attention_heads: Determine the number of attention heads in
	        the model

	Returns:
	    A tensor of shape (batch_size, num_attention_heads, 1,
	    sequence_length)
	"""
	batch_size, sequence_length = attention_mask.shape
	cp2 = 2 ** math.floor(math.log2(num_attention_heads))
	base = jnp.asarray(2 ** (-(2 ** -(math.log2(cp2) - 3))), dtype=jnp.float32)
	powers = jnp.arange(1, 1 + cp2, dtype=jnp.float32)
	slops = jnp.power(base, powers)
	if cp2 != num_attention_heads:
		extra_base = jnp.asarray(
			2 ** (-(2 ** -(math.log2(2 * cp2) - 3))), dtype=jnp.float32
		)
		num_rem_heads = min(cp2, num_attention_heads - cp2)
		extra_power = jnp.arange(1, 1 + 2 * num_rem_heads, 2, dtype=jnp.dtype)
		slops = jnp.concatenate([slops, jnp.power(extra_base, extra_power)], axis=0)
	arange_tensor = (((jnp.cumsum(attention_mask, axis=-1)) - 1) * attention_mask)[
		:, jnp.newaxis, :
	]
	alibi = slops[..., jnp.newaxis].astype(jnp.bfloat16) * arange_tensor
	return alibi.reshape(batch_size, num_attention_heads, 1, sequence_length)


def dropout_add(
	linen_drop: flax.linen.Dropout,
	x: chex.Array,
	residual: chex.Array,
	deterministic: bool,
) -> chex.Array:
	"""The dropout_add function is a helper function that adds the residual to the output of
	the dropout layer. This is necessary because we want to use deterministic=True when
	we are evaluating our model, but we still need to add in the residual. The reason for this
	is that during training, we have two paths through our network: one with dropout and one without.
	The path without dropout (residual) allows us to backpropagate gradients through both paths at once.

	Args:
	    linen_drop: flax.linen.Dropout: Specify the dropout layer
	    x: chex.Array: Pass in the input to the dropout layer
	    residual: chex.Array: Add the residual to the output of
	        dropout_add
	    deterministic: bool: Determine whether the dropout layer is
	        active or not

	Returns:
	    A tensor that is the sum of the residual and a dropout layer
	"""
	out = linen_drop(inputs=x, deterministic=deterministic)
	out = residual + out
	return out


class FlaxFalconRotaryEmbedding(nn.Module):
	dtype: jnp.dtype = jnp.float32

	def __call__(self, query, key, frequencies, position_ids):
		sin, cos = frequencies

		sin = sin[position_ids][:, jnp.newaxis, :, :]
		cos = cos[position_ids][:, jnp.newaxis, :, :]

		query = apply_rotary_pos_emb(query, sin, cos)
		key = apply_rotary_pos_emb(key, sin, cos)
		return query.astype(self.dtype), key.astype(self.dtype)


class FlaxFalconAttention(FlaxAttentionModule):
	"""
	Implements the attention mechanism for the Falcon model.

	This attention mechanism supports multiple variants depending on the configuration:
	- Multi-Query Attention: Uses a single head for keys and values and multiple heads for queries.
	- New Decoder Architecture: Uses a different arrangement of heads for queries, keys, and values.

	Attributes:
	    config (FalconConfig): Configuration for the attention module.
	    dtype (jnp.dtype): Data type for computations (default: jnp.float32).
	    param_dtype (jnp.dtype): Data type for parameters (default: jnp.float32).
	    precision (Optional[Union[jax.lax.Precision, str]]): Precision setting for JAX operations.

	"""

	config: FalconConfig
	dtype: jnp.dtype = jnp.float32
	param_dtype: jnp.dtype = jnp.float32
	precision: Optional[Union[jax.lax.Precision, str]] = None

	def setup(self) -> None:
		config = self.config
		head_dim = config.hidden_size // config.num_attention_heads
		if config.new_decoder_architecture:
			qkv_out_dim = (config.num_kv_heads * 2 + config.num_attention_heads) * head_dim
		elif config.multi_query:
			qkv_out_dim = config.hidden_size + 2 * head_dim
		else:
			qkv_out_dim = 3 * config.hidden_size

		self.head_dim = head_dim
		assert self.head_dim * config.num_attention_heads == config.hidden_size
		self.num_kv_heads = (
			config.num_kv_heads
			if (config.new_decoder_architecture or not config.multi_query)
			else 1
		)
		self.new_decoder_architecture = config.new_decoder_architecture
		self.num_heads = config.num_attention_heads
		self.query_key_value = Dense(
			features=qkv_out_dim,
			dtype=self.dtype,
			param_dtype=self.param_dtype,
			precision=self.precision,
			use_bias=config.bias,
			**get_dot_general_by_bits(config.bits, config.easy_method),
		)
		self.inv_norm_factor = 1 / math.sqrt(head_dim)
		self.dense = Dense(
			features=config.hidden_size,
			dtype=self.dtype,
			param_dtype=self.param_dtype,
			use_bias=config.bias,
			precision=self.precision,
			**get_dot_general_by_bits(config.bits, config.easy_method),
		)
		self.rotary = FlaxFalconRotaryEmbedding(self.dtype)
		self.attention_performer = FlexibleAttentionModule(
			attention_dropout=0.0,
			num_attention_heads=config.num_attention_heads,
			head_dims=self.head_dim,
			precision=self.precision,
			force_float32_tpu=True,
			attn_mechanism=config.attn_mechanism,
			dtype=self.config.attn_dtype,
			mesh=config.mesh,
			sm_scale=self.inv_norm_factor,
			axis_name=config.attention_axis_name,
			base_config=config,
			_do_check=False,
		)

	def _split_heads(self, qkv: chex.Array) -> Tuple[chex.Array, chex.Array, chex.Array]:
		"""
		Splits the query, key, and value tensors into separate heads.

		Args:
		    qkv (chex.Array): Combined query, key, and value tensor.

		Returns:
		    Tuple[chex.Array, chex.Array, chex.Array]: A tuple containing the query, key, and value tensors split into heads.
		"""
		batch_size, sequence_length, _ = qkv.shape

		if self.config.new_decoder_architecture:
			qkv = qkv.reshape(
				batch_size,
				sequence_length,
				-1,
				self.num_heads // self.num_kv_heads + 2,
				self.head_dim,
			)
			query_states = qkv[:, :, :, :-2]
			key_states = qkv[:, :, :, [-2]]
			value_states = qkv[:, :, :, [-1]]
			key_states = jnp.broadcast_to(key_states, query_states.shape)
			value_states = jnp.broadcast_to(value_states, query_states.shape)

			query_states, key_states, value_states = [
				x.reshape(x.shape[:-2] + (x.shape[-2] * x.shape[-1],))
				for x in (query_states, key_states, value_states)
			]

			return query_states, key_states, value_states
		if self.config.multi_query:
			qkv = qkv.reshape(
				batch_size, sequence_length, self.config.num_attention_heads + 2, -1
			)
			query_states, key_states, value_states = (
				qkv[..., :-2, :],
				qkv[..., [-2], :],
				qkv[..., [-1], :],
			)

		else:
			query_states, key_states, value_states = jnp.split(qkv, 3, -1)
		return query_states, key_states, value_states

	def _merge_heads(self, x: chex.Array) -> chex.Array:
		"""
		Merges the attention heads into a single tensor.

		Args:
		    x (chex.Array): Tensor with separate attention heads.

		Returns:
		    chex.Array: Tensor with merged attention heads.
		"""
		batch_size_and_num_heads, seq_length, _ = x.shape
		batch_size = batch_size_and_num_heads // self.num_heads
		x = x.reshape(
			batch_size, self.config.num_attention_heads, seq_length, self.head_dim
		)
		return x.reshape(
			batch_size, seq_length, self.config.num_attention_heads * self.head_dim
		)

	def __call__(
		self,
		hidden_states: chex.Array,
		attention_mask: chex.Array,
		position_ids: chex.Array,
		causal_mask: chex.Array = None,
		segment_ids: Optional[chex.Array] = None,
		alibi: Optional[chex.Array] = None,
		frequencies: Tuple[chex.Array, chex.Array] = None,
		init_cache: bool = False,
		output_attentions: bool = False,
		deterministic: bool = False,
	):
		"""
		Forward pass of the attention module.

		Args:
		    hidden_states (chex.Array): Input hidden states.
		    attention_mask (chex.Array): Mask to apply on the attention scores.
		    position_ids (chex.Array): Position indices for the tokens.
		    causal_mask (chex.Array, optional): Causal mask for ensuring autoregressive behavior.
		    segment_ids (Optional[chex.Array], optional): Segment IDs for segment-based attention.
		    alibi (Optional[chex.Array], optional): Alibi tensor for adding positional bias.
		    frequencies (Tuple[chex.Array, chex.Array], optional): Cosine and sine components for rotary embeddings.
		    init_cache (bool, optional): If True, initializes cache for caching keys and values.
		    output_attentions (bool, optional): If True, outputs attention weights alongside the hidden states.
		    deterministic (bool, optional): If True, disables dropout for deterministic behavior.

		Returns:
		    Union[chex.Array, Tuple[chex.Array, chex.Array]]: The output tensor and optionally the attention weights.
		"""
		fused_qkv = self.query_key_value(
			hidden_states
		)  # [batch_size, seq_length, 3 x hidden_size]
		num_kv_heads = (
			self.num_heads if self.new_decoder_architecture else self.num_kv_heads
		)
		# 3 x [batch_size, seq_length, num_heads, head_dim]
		(query_layer, key_layer, value_layer) = self._split_heads(fused_qkv)
		batch_size, query_length, _, _ = query_layer.shape
		key_length = query_length
		query_layer = query_layer.reshape(
			batch_size,
			query_length,
			self.num_heads,
			self.head_dim,
		)
		key_layer = key_layer.reshape(
			batch_size,
			query_length,
			num_kv_heads,
			self.head_dim,
		)
		value_layer = value_layer.reshape(
			batch_size,
			query_length,
			num_kv_heads,
			self.head_dim,
		)

		if self.has_variable("cache", "cached_key"):
			mask_shift = self.variables["cache"]["cache_index"]
			max_decoder_length = self.variables["cache"]["cached_key"].shape[1]
			causal_mask = lax.dynamic_slice(
				causal_mask,
				(0, 0, mask_shift, 0),
				(1, 1, query_length, max_decoder_length),
			)
		else:
			causal_mask = causal_mask[:, :, :query_length, :key_length]

		causal_mask = jnp.broadcast_to(causal_mask, (batch_size,) + causal_mask.shape[1:])
		if attention_mask.ndim == 2:
			attention_mask = jnp.expand_dims(attention_mask, axis=(-3, -2))
		attention_mask = combine_masks(attention_mask, causal_mask)
		if alibi is None:
			query_layer, key_layer = map(
				lambda x: x.transpose(0, 2, 1, 3),
				[query_layer, key_layer],
			)  # noqa
			query_layer, key_layer = self.rotary(
				query_layer,
				key_layer,
				frequencies,
				position_ids,
			)
			query_layer, key_layer = map(
				lambda x: x.transpose(0, 2, 1, 3),
				[query_layer, key_layer],
			)  # noqa

		if self.has_variable("cache", "cached_key") or init_cache:
			key_layer, value_layer, attention_mask = self._concatenate_to_cache(
				key_layer, value_layer, query_layer, attention_mask
			)
			key_length = key_layer.shape[1]
		float_min = jnp.finfo(query_layer.dtype).min

		attention_bias = lax.select(
			attention_mask > 0,
			jnp.full(attention_mask.shape, 0.0).astype(self.dtype),
			jnp.full(attention_mask.shape, float_min).astype(self.dtype),
		)
		dtype = jnp.promote_types(key_layer.dtype, jnp.float32)

		query_layer, key_layer, value_layer, attention_bias = map(
			lambda x: x.astype(dtype=dtype),
			(query_layer, key_layer, value_layer, attention_bias),
		)

		if alibi is None:
			attention = self.attention_performer(
				query_states=query_layer,
				key_states=key_layer,
				value_states=value_layer,
				causal_mask=causal_mask,
				attention_mask=attention_mask,
				deterministic=deterministic,
				segment_ids=segment_ids,
				query_sequence_length=query_length,
				key_value_sequence_length=key_length,
				uses_cache=self.has_variable("cache", "cached_key") or init_cache,
				bias=attention_bias,
				causal=False,
			)
			attention_outputs = attention.attention_outputs
			attention_outputs = attention_outputs.reshape(
				batch_size, query_length, self.num_heads * self.head_dim
			)
			output_tensor = self.dense(attention_outputs)
			return output_tensor, attention.attention_weights

		else:
			attention_scores = jnp.einsum(
				"...qhd,...khd->...hqk",
				query_layer,
				key_layer,
				precision=self.precision,
			)
			attention_scores = attention_scores.reshape(
				batch_size, self.num_heads, query_length, key_length
			)
			attention_scores = attention_scores + alibi.reshape(
				batch_size, self.num_heads, 1, -1
			)
			attention_scores *= self.inv_norm_factor
			attention_scores = jax.nn.softmax(attention_scores + attention_bias, axis=-1)
			attention_scores = attention_scores.reshape(
				batch_size, self.num_heads, query_length, key_length
			)
			# matmul: [batch_size * num_heads, q_length, head_dim]
			attn_output = jax.lax.batch_matmul(
				attention_scores, value_layer.transpose(0, 2, 1, 3)
			)  # noqa
			attn_output = attn_output.reshape(
				(attn_output.shape[1] * attn_output.shape[0],) + attn_output.shape[2:]
			)
			attn_output = self._merge_heads(attn_output)
			if self.config.shard_attention_computation:
				attn_output = with_sharding_constraint(
					attn_output,
					PartitionSpec(
						("dp", "fsdp"),
						"sp" if attn_output.shape[1] != 1 else None,
						"tp",
					),
				)

			output_tensor = self.dense(attn_output)

			if output_attentions:
				return output_tensor, attention_scores
			return output_tensor, None


class FlaxFalconMlp(nn.Module):
	config: FalconConfig
	dtype: jnp.dtype = jnp.float32
	param_dtype: jnp.dtype = jnp.float32
	precision: Optional[Union[jax.lax.Precision, str]] = None

	def setup(self) -> None:
		self.dense_h_to_4h = Dense(
			features=self.config.ff_factor * self.config.hidden_size,
			dtype=self.dtype,
			precision=self.precision,
			param_dtype=self.param_dtype,
			use_bias=self.config.bias,
			**get_dot_general_by_bits(self.config.bits, self.config.easy_method),
		)
		self.dense_4h_to_h = Dense(
			features=self.config.hidden_size,
			dtype=self.dtype,
			param_dtype=self.param_dtype,
			precision=self.precision,
			use_bias=self.config.bias,
			**get_dot_general_by_bits(self.config.bits, self.config.easy_method),
		)

	def __call__(self, x: chex.Array, deterministic: bool = True):
		x = control_mlp_sharding(x, self.config.partition_axis)
		if (
			self.config.pallas_runtime
			and self.dense_4h_to_h.variables.get("params", None) is not None
		):
			return jax.vmap(
				functools.partial(
					falcon_mlp_pallas,
					blocksize_k=self.config.pallas_k_block_size,
					blocksize_m=self.config.pallas_m_block_size,
					blocksize_n=self.config.pallas_n_block_size,
					prod_dtype=self.dtype,
					precision=self.precision,
				),
				in_axes=(0, None, None),
			)(
				x,
				self.dense_h_to_4h.variables["params"]["kernel"],
				self.dense_4h_to_h.variables["params"]["kernel"],
			)
		return self.dense_4h_to_h(nn.gelu(self.dense_h_to_4h(x), approximate=False))


class FlaxFalconBlock(nn.Module):
	config: FalconConfig
	dtype: jnp.dtype = jnp.float32
	param_dtype: jnp.dtype = jnp.float32
	precision: Optional[Union[jax.lax.Precision, str]] = None

	def setup(self) -> None:
		config = self.config

		if config.new_decoder_architecture and config.num_ln_in_parallel_attn == 2:
			self.ln_attn = nn.LayerNorm(
				epsilon=config.layer_norm_epsilon,
				dtype=self.dtype,
				param_dtype=self.param_dtype,
			)
			self.ln_mlp = nn.LayerNorm(
				epsilon=config.layer_norm_epsilon,
				dtype=self.dtype,
				param_dtype=self.param_dtype,
			)
		else:
			self.input_layernorm = nn.LayerNorm(
				epsilon=config.layer_norm_epsilon,
				dtype=self.dtype,
				param_dtype=self.param_dtype,
			)
			if not config.parallel_attn:
				self.post_attention_layernorm = nn.LayerNorm(
					epsilon=config.layer_norm_epsilon,
					dtype=self.dtype,
					param_dtype=self.param_dtype,
				)
		attn_block = FlaxFalconAttention
		mlp_block = FlaxFalconMlp
		if self.config.gradient_checkpointing != "":
			# hidden_states: chex.Array, 0
			# attention_mask: chex.Array, 1
			# position_ids: chex.Array, 2
			# causal_mask: chex.Array = None, 3
			# segment_ids: Optional[chex.Array] = None, 4
			# alibi: Optional[chex.Array] = None, 5
			# frequencies: Tuple[chex.Array, chex.Array] = None, 6
			# init_cache: bool = False, 7
			# output_attentions: bool = False, 8
			# deterministic: bool = False, 9
			attn_block = flax.linen.partitioning.remat(
				attn_block,
				policy=get_gradient_checkpoint_policy(self.config.gradient_checkpointing),
				static_argnums=(1, 3, 6, 7, 8, 9),
			)

			mlp_block = flax.linen.partitioning.remat(
				mlp_block,
				policy=get_gradient_checkpoint_policy(self.config.gradient_checkpointing),
				static_argnums=(1,),
			)

		self.mlp = mlp_block(
			config=config,
			dtype=self.dtype,
			param_dtype=self.param_dtype,
			precision=self.precision,
		)
		self.self_attention = attn_block(
			config=config,
			dtype=self.dtype,
			param_dtype=self.param_dtype,
			precision=self.precision,
		)

		self.dropout = flax.linen.Dropout(self.config.attention_dropout)
		self.dropout_mlp = flax.linen.Dropout(self.config.hidden_dropout)

	def __call__(
		self,
		hidden_states: chex.Array,
		attention_mask: chex.Array,
		position_ids: chex.Array,
		causal_mask: chex.Array = None,
		segment_ids: Optional[chex.Array] = None,
		alibi: Optional[chex.Array] = None,
		frequencies: Tuple[chex.Array, chex.Array] = None,
		init_cache: bool = False,
		output_attentions: bool = False,
		deterministic: bool = False,
	):
		"""
		Forward pass of the FalconBlock module.

		Args:
		    hidden_states (chex.Array): Input hidden states.
		    attention_mask (chex.Array): Mask to apply on the attention scores.
		    position_ids (chex.Array): Position indices for the tokens.
		    causal_mask (chex.Array, optional): Causal mask for ensuring autoregressive behavior.
		    segment_ids (Optional[chex.Array], optional): Segment IDs for segment-based attention.
		    alibi (Optional[chex.Array], optional): Alibi tensor for adding positional bias.
		    frequencies (Tuple[chex.Array, chex.Array], optional): Cosine and sine components for rotary embeddings.
		    init_cache (bool, optional): If True, initializes cache for caching keys and values.
		    output_attentions (bool, optional): If True, outputs attention weights alongside the hidden states.
		    deterministic (bool, optional): If True, disables dropout for deterministic behavior.

		Returns:
		    Union[chex.Array, Tuple[chex.Array, chex.Array]]: The output tensor and optionally the attention weights.
		"""
		residual = hidden_states

		if self.config.num_ln_in_parallel_attn == 2:
			attention_layernorm_out = self.ln_attn(hidden_states)
			mlp_layernorm_out = self.ln_mlp(hidden_states)
		else:
			attention_layernorm_out = self.input_layernorm(hidden_states)

		# hidden_states: chex.Array,
		# attention_mask: chex.Array,
		# position_ids: chex.Array,
		# causal_mask: chex.Array = None,
		# segment_ids: Optional[chex.Array] = None,
		# alibi: Optional[chex.Array] = None,
		# frequencies: Tuple[chex.Array, chex.Array] = None,
		# init_cache: bool = False,
		# output_attentions: bool = False,
		# deterministic: bool = False,
		attention_output, attn_score = self.self_attention(
			attention_layernorm_out,
			attention_mask,
			position_ids,
			causal_mask,
			segment_ids,
			alibi,
			frequencies,
			init_cache,
			output_attentions,
			deterministic,
		)

		if self.config.num_ln_in_parallel_attn == 1:
			if self.config.parallel_attn:
				mlp_layernorm_out = attention_layernorm_out
			else:
				residual = dropout_add(self.dropout, attention_output, residual, deterministic)
				mlp_layernorm_out = self.post_attention_layernorm(residual)

		if self.config.use_scan_mlp:
			mlp_output = block_wise_ffn(
				self.mlp,
				mlp_layernorm_out,
				self.config.scan_mlp_chunk_size,
				deterministic,
			)
		else:
			mlp_output = self.mlp(
				mlp_layernorm_out,
				deterministic,
			)

		if self.config.new_decoder_architecture or self.config.parallel_attn:
			mlp_output += attention_output

		output = dropout_add(self.dropout_mlp, mlp_output, residual, deterministic)
		return output, attn_score


class FlaxFalconCollection(nn.Module):
	"""
	Represents a collection of FlaxFalconBlock modules, forming the main body of the Falcon model.

	This module stacks multiple FlaxFalconBlock layers and handles the forward pass through them,
	optionally collecting hidden states and attention scores.

	Attributes:
	    config (FalconConfig): Configuration for the Falcon model.
	    dtype (jnp.dtype): Data type for computations (default: jnp.float32).
	    param_dtype (jnp.dtype): Data type for parameters (default: jnp.float32).
	    precision (Optional[Union[jax.lax.Precision, str]]): Precision setting for JAX operations.
	"""

	config: FalconConfig
	dtype: jnp.dtype = jnp.float32
	param_dtype: jnp.dtype = jnp.float32
	precision: Optional[Union[jax.lax.Precision, str]] = None

	def setup(self) -> None:
		self.layers = [
			FlaxFalconBlock(
				config=self.config,
				dtype=self.dtype,
				param_dtype=self.param_dtype,
				precision=self.precision,
				name=str(i),
			)
			for i in range(self.config.num_hidden_layers)
		]

	def __call__(
		self,
		hidden_states: chex.Array,
		attention_mask: chex.Array,
		frequencies: Tuple[chex.Array, chex.Array],
		position_ids: chex.Array,
		causal_mask: chex.Array,
		alibi: Optional[chex.Array] = None,
		segment_ids: Optional[chex.Array] = None,
		output_attentions: bool = False,
		output_hidden_states: bool = False,
		init_cache: bool = False,
		deterministic: bool = True,
	):
		"""
		Forward pass through the collection of FlaxFalconBlock layers.

		Args:
		    hidden_states (chex.Array): Input hidden states.
		    attention_mask (chex.Array): Mask to apply on the attention scores.
		    frequencies (Tuple[chex.Array, chex.Array]): Cosine and sine components for rotary embeddings.
		    position_ids (chex.Array): Position indices for the tokens.
		    causal_mask (chex.Array): Causal mask for ensuring autoregressive behavior.
		    alibi (Optional[chex.Array], optional): Alibi tensor for adding positional bias.
		    segment_ids (Optional[chex.Array], optional): Segment IDs for segment-based attention.
		    output_attentions (bool, optional): If True, returns attention scores from each layer.
		    output_hidden_states (bool, optional): If True, returns hidden states from each layer.
		    init_cache (bool, optional): If True, initializes cache for caching keys and values.
		    deterministic (bool, optional): If True, disables dropout for deterministic behavior.

		Returns:
		    Tuple[chex.Array, Tuple[chex.Array], Tuple[chex.Array]]: A tuple containing:
		        - The final hidden states.
		        - A tuple of hidden states from all layers (if output_hidden_states is True).
		        - A tuple of attention scores from all layers (if output_attentions is True).
		"""
		all_hidden_states = () if output_hidden_states else None
		all_attentions = () if output_attentions else None
		for layer in self.layers:
			hidden_states, score = layer(
				hidden_states=hidden_states,
				alibi=alibi,
				attention_mask=attention_mask,
				frequencies=frequencies,
				position_ids=position_ids,
				causal_mask=causal_mask,
				init_cache=init_cache,
				output_attentions=output_attentions,
				deterministic=deterministic,
				segment_ids=segment_ids,
			)
			if output_hidden_states:
				all_hidden_states += (hidden_states,)
			if output_attentions:
				all_attentions += (score,)

		return hidden_states, all_hidden_states, all_attentions


class FlaxFalconModule(nn.Module):
	config: FalconConfig
	dtype: jnp.dtype = jnp.float32
	param_dtype: jnp.dtype = jnp.float32
	precision: Optional[Union[jax.lax.Precision, str]] = None

	def setup(self) -> None:
		config = self.config
		self.word_embeddings = nn.Embed(
			num_embeddings=config.vocab_size,
			features=config.hidden_size,
			dtype=self.dtype,
			param_dtype=self.param_dtype,
		)
		self.h = FlaxFalconCollection(
			config=self.config,
			dtype=self.dtype,
			param_dtype=self.param_dtype,
			precision=self.precision,
		)
		self.ln_f = nn.LayerNorm(
			dtype=self.dtype,
			param_dtype=self.param_dtype,
			epsilon=config.layer_norm_epsilon,
		)
		self.causal_mask = flax.linen.make_causal_mask(
			jnp.ones(
				shape=(1, self.config.granted_mask_max_position_embedding),
				dtype="bool",
			),
			dtype="bool",
		)
		self.frequencies = None
		if not self.config.alibi:
			initial_rope_kwargs = dict(rope_type="none")
			if config.rope_scaling is not None:
				scaling_type = config.rope_scaling["type"]
				scaling_factor = config.rope_scaling["factor"]
				initial_rope_kwargs = dict(
					scaling_factor=scaling_factor, rope_type=scaling_type
				)
			self.frequencies = precompute_frequencies(
				max_position_embeddings=self.config.granted_freq_max_position_embedding,
				dim=config.hidden_size // config.num_attention_heads,
				base=config.rope_theta,
				**initial_rope_kwargs,
			)

	def __call__(
		self,
		input_ids: Optional[chex.Array] = None,
		attention_mask: Optional[chex.Array] = None,
		position_ids: Optional[chex.Array] = None,
		segment_ids: Optional[chex.Array] = None,
		input_embeds: Optional[chex.Array] = None,
		output_attentions: Optional[bool] = None,
		output_hidden_states: Optional[bool] = None,
		init_cache: bool = False,
		deterministic: bool = True,
		return_dict: bool = True,
	) -> Union[FlaxBaseModelOutput, Tuple]:
		"""
		Forward pass through the Falcon module.

		Args:
		    input_ids (chex.Array): Input tensor containing token IDs.
		    attention_mask (chex.Array): Mask for attention.
		    position_ids (chex.Array): Positional indices.
		    segment_ids (Optional[chex.Array]): Segment IDs for different input parts.
		    input_embeds (Optional[chex.Array]): Embedded input tensor.
		    output_attentions (Optional[bool]): If True, output attention weights.
		    output_hidden_states (Optional[bool]): If True, output hidden states.
		    init_cache (bool): If True, initialize cache for decoding.
		    deterministic (bool): If True, disable dropout.
		    return_dict (bool): If True, return a dictionary of outputs.

		Returns:
		    FlaxBaseModelOutput | Tuple: Model output, either as a named tuple or a standard tuple.
		"""
		if input_embeds is None and input_ids is not None:
			input_embeds = self.word_embeddings(input_ids.astype("i4"))
		else:
			raise ValueError("you should specify input_embeds or input_ids one of them")
		batch_size, sequence_length, _ = input_embeds.shape

		alibi = None
		if self.config.alibi:
			alibi = built_bloom_alibi(
				attention_mask,
				self.config.num_attention_heads,
			).astype(input_embeds.dtype)
		elif position_ids is None:
			position_ids = jnp.arange(0, sequence_length).reshape(1, -1)
		if attention_mask is None:
			attention_mask = jnp.ones((batch_size, sequence_length), dtype="i4")
		if attention_mask.ndim == 2:
			attention_mask = jnp.expand_dims(attention_mask, (-3, -2))

		hidden_states, all_hidden_states, all_attentions = self.h(
			hidden_states=input_embeds,
			attention_mask=attention_mask,
			position_ids=position_ids,
			alibi=alibi,
			frequencies=self.frequencies,
			causal_mask=self.causal_mask,
			output_attentions=output_attentions,
			deterministic=deterministic,
			output_hidden_states=output_hidden_states,
			init_cache=init_cache,
			segment_ids=segment_ids,
		)
		hidden_states = self.ln_f(hidden_states)
		if all_hidden_states is not None:
			all_hidden_states += hidden_states
		if return_dict:
			return FlaxBaseModelOutput(
				last_hidden_state=hidden_states,
				attentions=all_attentions,
				hidden_states=all_hidden_states,
			)
		else:
			return tuple(
				[s for s in [hidden_states, all_attentions, all_attentions] if s is not None]
			)


class FlaxFalconPretrainedModel(EDPretrainedModel):
	module_class: nn.Module = None
	config_class = FalconConfig

	def __init__(
		self,
		config,
		_do_init=False,
		dtype: jnp.dtype = jnp.float32,
		param_dtype: jnp.dtype = jnp.float32,
		input_shape: Tuple = (1, 1),
		precision: Optional[Union[str, jax.lax.Precision]] = None,
	):
		module = self.module_class(
			config=config,
			dtype=dtype,
			param_dtype=param_dtype,
			precision=precision,
		)
		super().__init__(
			_do_init=_do_init,
			module=module,
			config=config,
			dtype=dtype,
			input_shape=input_shape,
		)

	def init_weights(
		self,
		rng: jax.random.PRNGKey,
		input_shape: Tuple,
		params: FrozenDict = None,
	) -> FrozenDict:
		"""The init_weights function is used to initialize the weights of a model.

		Args:
		    self: Access variables that belong to the class
		    rng: jax.random.PRNGKey: Initialize the weights of the model
		    input_shape: Tuple: Specify the shape of the input tensor
		    params: FrozenDict: Pass in the parameters of a pre-trained
		        model

		Returns:
		    A frozendict of parameters
		"""
		input_ids = jnp.zeros(input_shape, dtype="i4")
		attention_mask = jnp.ones_like(input_ids)
		position_ids = jnp.broadcast_to(
			jnp.arange(jnp.atleast_2d(input_ids).shape[-1]), input_shape
		)
		params_rng, dropout_rng = jax.random.split(rng)
		rngs = {"params": params_rng, "dropout": dropout_rng}

		if self.config.add_cross_attention:
			encoder_hidden_states = jnp.zeros(input_shape + (self.config.hidden_size,))
			encoder_attention_mask = attention_mask
			module_init_outputs = self.module.init(
				rngs,
				input_ids,
				attention_mask,
				position_ids,
				encoder_hidden_states,
				encoder_attention_mask,
				return_dict=False,
			)
		else:
			module_init_outputs = self.module.init(
				rngs, input_ids, attention_mask, position_ids, return_dict=False
			)

		random_params = module_init_outputs["params"]

		if params is not None:
			random_params = flatten_dict(unfreeze(random_params))
			params = flatten_dict(unfreeze(params))
			for missing_key in self._missing_keys:
				params[missing_key] = random_params[missing_key]
			self._missing_keys = set()
			return freeze(unflatten_dict(params))
		else:
			return random_params

	def __call__(
		self,
		input_ids: Optional[chex.Array] = None,
		input_embeds: Optional[chex.Array] = None,
		attention_mask: Optional[chex.Array] = None,
		position_ids: Optional[chex.Array] = None,
		segment_ids: Optional[chex.Array] = None,
		params: dict = None,
		past_key_values: Optional[dict] = None,
		dropout_rng: jax.random.PRNGKey = None,
		train: bool = False,
		output_attentions: Optional[bool] = None,
		output_hidden_states: Optional[bool] = None,
		return_dict: Optional[bool] = None,
		add_params_field: bool = False,
		**kwargs,
	):
		"""
		Forward pass through the model.

		Args:
		    input_ids (chex.Array): Input tensor containing token IDs.
		    input_embeds (Optional[chex.Array]): embedding inputs to be used instead of input_ids.
		    attention_mask (Optional[chex.Array]): Mask for attention.
		    position_ids (Optional[chex.Array]): Positional indices.
		    segment_ids (Optional[chex.Array]): Segment IDs for distinguishing different parts of the input.
		    params (dict, optional): Parameters for the model.
		    past_key_values (dict, optional): Past key and value states for caching.
		    dropout_rng (jax.random.PRNGKey, optional): RNG key for dropout.
		    train (bool): If True, the model is in training mode.
		    output_attentions (Optional[bool]): If True, output attention weights.
		    output_hidden_states (Optional[bool]): If True, output hidden states.
		    return_dict (Optional[bool]): If True, return a dictionary of outputs.
		    add_params_field (bool): If True, include the parameters in the input dictionary.
		    **kwargs: Additional arguments.

		Returns:
		    Output type depends on the model configuration.
		"""
		output_attentions = (
			output_attentions
			if output_attentions is not None
			else self.config.output_attentions
		)
		output_hidden_states = (
			output_hidden_states
			if output_hidden_states is not None
			else self.config.output_hidden_states
		)
		return_dict = return_dict if return_dict is not None else self.config.return_dict
		batch_size, sequence_length = (
			input_ids.shape if input_ids is not None else input_embeds.shape[:2]
		)

		if position_ids is None:
			if past_key_values is not None:
				raise ValueError(
					"Make sure to provide `position_ids` when passing `past_key_values`."
				)

			position_ids = jnp.broadcast_to(
				jnp.arange(sequence_length)[None, :], (batch_size, sequence_length)
			)

		if attention_mask is None:
			attention_mask = jnp.ones((batch_size, sequence_length))
		rng_s = {}
		if dropout_rng is not None:
			rng_s["dropout"] = dropout_rng

		inputs = (
			{"params": params or self.params} if add_params_field else params or self.params
		)

		if self.config.bits is not None:
			rng_s["params"] = jax.random.key(0)
		if past_key_values is not None:
			inputs["cache"] = past_key_values
			mutable = ["cache"]
		else:
			mutable = False

		outputs = self.module.apply(
			inputs,
			input_ids=jnp.array(input_ids, dtype="i4"),
			attention_mask=jnp.array(attention_mask, dtype="i4"),
			position_ids=jnp.array(position_ids, dtype="i4"),
			deterministic=not train,
			init_cache=False,
			output_attentions=output_attentions,
			output_hidden_states=output_hidden_states,
			return_dict=return_dict,
			input_embeds=input_embeds,
			segment_ids=segment_ids,
			rngs=rng_s,
			mutable=mutable,
		)

		if past_key_values is not None and return_dict:
			outputs, past_key_values = outputs
			outputs["past_key_values"] = unfreeze(past_key_values["cache"])
			return outputs
		elif past_key_values is not None and not return_dict:
			outputs, past_key_values = outputs
			outputs = outputs[:1] + (unfreeze(past_key_values["cache"]),) + outputs[1:]
		return outputs

	def init_cache(self, batch_size, max_length):

		return super().init_cache(batch_size=batch_size, max_length=max_length)

	def prepare_inputs_for_generation(
		self, input_ids, max_length, attention_mask: Optional[chex.Array] = None
	):
		batch_size, seq_length = input_ids.shape

		past_key_values = self.init_cache(batch_size, max_length)
		extended_attention_mask = jnp.ones((batch_size, max_length), dtype="i4")
		if attention_mask is not None:
			position_ids = attention_mask.cumsum(axis=-1) - 1
			extended_attention_mask = lax.dynamic_update_slice(
				extended_attention_mask, attention_mask, (0, 0)
			)
		else:
			position_ids = jnp.broadcast_to(
				jnp.arange(seq_length, dtype="i4")[None, :], (batch_size, seq_length)
			)

		return {
			"past_key_values": past_key_values,
			"attention_mask": extended_attention_mask,
			"position_ids": position_ids,
		}

	def update_inputs_for_generation(self, model_outputs, model_kwargs):
		model_kwargs["past_key_values"] = model_outputs.past_key_values
		model_kwargs["position_ids"] = model_kwargs["position_ids"][:, -1:] + 1
		return model_kwargs


class FlaxFalconModel(FlaxFalconPretrainedModel):
	module_class = FlaxFalconModule

	def get_input_embeddings(self):
		return self.module.word_embeddings

	def set_input_embeddings(self, value):
		self.module.word_embeddings = value


class FlaxFalconForCausalLMModule(nn.Module):
	config: FalconConfig
	dtype: jnp.dtype = jnp.float32
	param_dtype: jnp.dtype = jnp.float32
	precision: Optional[Union[jax.lax.Precision, str]] = None

	def setup(self) -> None:
		self.transformer = FlaxFalconModule(
			config=self.config,
			dtype=self.dtype,
			param_dtype=self.param_dtype,
			precision=self.precision,
		)

		self.lm_head = Dense(
			self.config.vocab_size,
			use_bias=False,
			**get_dot_general_by_bits(self.config.bits, self.config.easy_method),
		)

	def __call__(
		self,
		input_ids: Optional[chex.Array] = None,
		attention_mask: Optional[chex.Array] = None,
		position_ids: Optional[chex.Array] = None,
		segment_ids: Optional[chex.Array] = None,
		input_embeds: Optional[chex.Array] = None,
		output_attentions: Optional[bool] = None,
		output_hidden_states: Optional[bool] = None,
		init_cache: bool = False,
		deterministic: bool = True,
		return_dict: bool = True,
	) -> Union[FlaxCausalLMOutput, Tuple]:
		"""
		Forward pass through the Falcon module.

		Args:
		    input_ids (Optional[chex.Array]): Input tensor containing token IDs.
		    attention_mask (Optional[chex.Array]): Mask for attention.
		    position_ids (Optional[chex.Array]): Positional indices.
		    segment_ids (Optional[chex.Array]): Segment IDs for different input parts.
		    input_embeds (Optional[chex.Array]): Embedded input tensor.
		    output_attentions (Optional[bool]): If True, output attention weights.
		    output_hidden_states (Optional[bool]): If True, output hidden states.
		    init_cache (bool): If True, initialize cache for decoding.
		    deterministic (bool): If True, disable dropout.
		    return_dict (bool): If True, return a dictionary of outputs.

		Returns:
		    FlaxCausalLMOutput | Tuple: Model output, either as a named tuple or a standard tuple.
		"""
		transformer_output = self.transformer(
			input_ids=input_ids,
			attention_mask=attention_mask,
			position_ids=position_ids,
			output_attentions=output_attentions,
			init_cache=init_cache,
			return_dict=return_dict,
			input_embeds=input_embeds,
			deterministic=deterministic,
			output_hidden_states=output_hidden_states,
			segment_ids=segment_ids,
		)
		if return_dict:
			hidden_state = transformer_output.last_hidden_state
		else:
			hidden_state = transformer_output[0]
		output = self.lm_head(hidden_state)
		if return_dict:
			if output_attentions:
				return FlaxCausalLMOutput(
					logits=output, attentions=transformer_output.attentions
				)
			else:
				return FlaxCausalLMOutput(
					logits=output,
				)
		else:
			return (output, transformer_output[1]) if output_attentions else (output,)


class FlaxFalconForCausalLM(FlaxFalconPretrainedModel):
	module_class = FlaxFalconForCausalLMModule

	def get_decoder(self):
		return self.module.transformer

	def get_output_embeddings(self):
		return self.module.lm_head

	def get_input_embeddings(self):
		return self.module.transformer.word_embeddings

	def set_input_embeddings(self, value):
		self.module.transformer.word_embeddings = value

	def set_decoder(self, decoder):
		self.module.transformer = decoder

	def set_output_embeddings(self, new_embeddings):
		self.module.lm_head = new_embeddings
