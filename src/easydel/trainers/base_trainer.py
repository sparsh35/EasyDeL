
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

import abc
import os
import pprint
import sys
import threading
import time
from abc import abstractmethod
from dataclasses import dataclass
from glob import glob
from typing import Any, Callable, Iterator, Literal, Mapping, Optional, Union

import jax
import numpy as np
from fjformer.checkpoint import CheckpointManager
from flax.core import unfreeze
from jax.sharding import Mesh
from optax import GradientTransformation, Schedule

try:
	import wandb  # noqa: F821 # type:ignore
except ImportError:
	wandb = None

from easydel.etils.etils import get_logger
from easydel.modules.modeling_utils import (
	EDPretrainedConfig,
	EDPretrainedModel,
)
from easydel.smi import get_capacity_matrix, initialise_tracking
from easydel.trainers.training_configurations import TrainArguments
from easydel.utils import Timers

logger = get_logger(__name__)


@dataclass
class TrainerConfigureDataloaderOutput:
	dataloader_train: Iterator[np.ndarray]
	max_training_steps: int
	dataloader_eval: Optional[Iterator[np.ndarray]] = None
	max_evaluation_steps: Optional[int] = None


@dataclass
class TrainerConfigureModelOutput:
	model: EDPretrainedModel
	tx: GradientTransformation
	scheduler: Schedule
	config: Optional[EDPretrainedConfig] = None


@dataclass
class TrainerConfigureFunctionOutput:
	create_sharded_state_from_params_function: Callable
	sharded_train_step_function: Callable
	mesh: Mesh
	checkpoint_manager: CheckpointManager
	initialize_state_function: Callable
	sharded_eval_step_function: Optional[Callable] = None


class BaseTrainer(abc.ABC):
	def __init__(
		self,
		arguments: TrainArguments,
		dataset_train: "Dataset",  # noqa: F821 # type:ignore
		dataset_eval: Optional["Dataset"] = None,  # noqa: F821 # type:ignore
		finetune: bool = True,
		checkpoint_path: Optional[Union[str, os.PathLike]] = None,
		_do_init_fns: bool = True,
	):
		self.arguments = arguments
		self.dataset_train = dataset_train
		self.dataset_eval = dataset_eval
		self.finetune = finetune
		self.checkpoint_path = checkpoint_path
		self.dtype = arguments.dtype
		self.param_dtype = arguments.param_dtype
		self._initialize_attributes()

		if _do_init_fns:
			self.initialize_trainer_utils()
		else:
			logger.warn(
				"You have set `_do_init_fns = False`. Functions will not be initialized automatically. "
				"Call `trainer.initialize_trainer_utils()` manually.",
			)

		if self.arguments.track_memory:
			self._initialize_memory_tracking()

	def _initialize_attributes(self):
		# Initialize all attributes with default values
		self.timer = getattr(self, "timer", None)
		self.wandb_runtime = getattr(self, "wandb_runtime", None)
		self.dataloader_train = getattr(self, "dataloader_train", None)
		self.dataloader_eval = getattr(self, "dataloader_eval", None)
		self.max_training_steps = getattr(self, "max_training_steps", None)
		self.max_evaluation_steps = getattr(self, "max_evaluation_steps", None)
		self.model = getattr(self, "model", None)
		self.config = getattr(self, "config", None)
		self.scheduler = getattr(self, "scheduler", None)
		self.tx = getattr(self, "tx", None)
		self.model_state = getattr(self, "model_state", None)
		self.rapture = self.arguments.rapture
		self.lora_parameters = getattr(self, "lora_parameters", None)
		self.lora_model = getattr(self, "lora_model", None)
		self.lora_tx = getattr(self, "lora_tx", None)
		self.lora_opt_state = getattr(self, "lora_opt_state", None)
		self.lora_apply_fn = getattr(self, "lora_apply_fn", None)
		self.create_sharded_state_from_params_function = getattr(
			self, "create_sharded_state_from_params_function", None
		)
		self.sharded_train_step_function = getattr(
			self, "sharded_train_step_function", None
		)
		self.sharded_eval_step_function = getattr(self, "sharded_eval_step_function", None)
		self.initialize_state_function = getattr(self, "initialize_state_function", None)
		self.mesh = getattr(self, "mesh", None)
		self.checkpoint_manager = getattr(self, "checkpoint_manager", None)
		self.state_shape = getattr(self, "state_shape", None)
		self.state_partition_spec = getattr(self, "state_partition_spec", None)
		self.state_named_sharding = getattr(self, "state_named_sharding", None)
		self.sharded_state = getattr(self, "sharded_state", None)
		self.pruning_module = getattr(self.arguments, "pruning_module", None)

	def _initialize_memory_tracking(self):
		if not self.arguments.performance_mode:
			initialise_tracking()
			self.arguments._stop_capturing_memory = False
			self._start_capturing_memory().start()

	def __str__(self):
		return pprint.pformat(self.__dict__, indent=2)

	__repr__ = __str__

	@staticmethod
	def finish():
		if wandb is not None:
			wandb.finish()

	def _start_capturing_memory(
		self,
		dir_prefix: str = "/dev/shm" if sys.platform != "win32" else ".",
	):
		def _start():
			try:
				while not self.arguments._stop_capturing_memory:
					information_queries = {
						f"accelerators/{device.replace('_', ' ')} ({key})": float(
							info[key].replace("%", "").replace("GB", "")
						)
						for key in ["Used", "Usage Percent"]
						for device, info in get_capacity_matrix(dir_prefix=dir_prefix).items()
					}
					self.arguments._captured_memory = information_queries
					time.sleep(1.5)
			except FileNotFoundError as err:
				if "directory: 'go'" in err.__str__():
					logger.warn(
						"in order to capture memory you need to have `go-lang` already installed.(ignoring memory capture action)"
					)
				else:
					raise FileNotFoundError(err) from err

		return threading.Thread(target=_start)

	def initialize_trainer_utils(self):
		"""
		Initializes various utilities used by the trainer.

		This includes setting up Weights & Biases, initializing the training timer,
		configuring dataloaders, configuring the model and optimizer, sharding the
		model and reference model states, and configuring the training and evaluation functions.
		"""
		self._initialize_wandb()
		self._initialize_timer()
		self._configure_dataloaders()
		self._configure_model()
		self._configure_functions()

	def _initialize_wandb(self):
		if self.arguments.use_wandb:
			self.wandb_runtime = self.arguments.get_wandb_init()

	def _initialize_timer(self):
		self.timer = Timers(use_wandb=False, tensorboard_writer=self.arguments.get_board())

	def _configure_dataloaders(self):
		"""
		Configures the dataloaders for training and evaluation.

		This method retrieves the dataloaders from the `configure_dataloaders` method,
		sets the maximum training and evaluation steps, and logs the time taken for
		this configuration.
		"""
		with self.timer("configure dataloaders"):
			dataset_configurations = self.configure_dataloaders()
			self.dataloader_train = dataset_configurations.dataloader_train
			self.max_training_steps = dataset_configurations.max_training_steps
			self.dataloader_eval = dataset_configurations.dataloader_eval
			self.max_evaluation_steps = dataset_configurations.max_evaluation_steps
		self.timer.log("configure dataloaders")

	def _configure_model(self):
		"""
		Configures the model, optimizer, scheduler, and configuration.

		This method retrieves the model, optimizer, scheduler, and configuration from
		the `configure_model` method and configures LoRA (if enabled). It also logs
		the time taken for this configuration.
		"""
		with self.timer("configure Model, Optimizer, Scheduler and Config"):
			model_configurations = self.configure_model()
			self.model = model_configurations.model
			self.tx = model_configurations.tx
			self.scheduler = model_configurations.scheduler
			self.config = model_configurations.config
			self._configure_lora()
		self.timer.log("configure Model, Optimizer, Scheduler and Config")

	def _configure_lora(self):
		"""
		Configures LoRA (Low-Rank Adaptation) if enabled in the training arguments.

		This method applies LoRA to the model, sets up the LoRA parameters, apply function,
		optimizer state, model, and optimizer, and logs the time taken for this configuration.
		"""
		if self.rapture is not None:
			lora_modules = self.rapture.apply_lora(
				module=self.model,
				parameters=self.arguments.rapture_config.parameters,
				tx=self.tx,
			)
			self.lora_parameters = lora_modules.lora_parameters
			self.lora_apply_fn = lora_modules.lora_module.__call__
			self.lora_opt_state = lora_modules.lora_opt_state
			self.lora_model = lora_modules.lora_module
			self.lora_tx = lora_modules.lora_tx

	def _configure_functions(self):
		"""
		Configures and JIT-compiles the training and evaluation step functions.

		This method retrieves the configured functions from the `configure_functions`
		method, sets up the mesh, checkpoint manager, and state initialization
		function, and logs the time taken for this configuration.
		"""
		with self.timer("configure functions and sharding them"):
			function_configurations = self.configure_functions()
			self.create_sharded_state_from_params_function = (
				function_configurations.create_sharded_state_from_params_function
			)
			self.sharded_train_step_function = (
				function_configurations.sharded_train_step_function
			)
			self.sharded_eval_step_function = (
				function_configurations.sharded_eval_step_function
			)
			self.mesh = function_configurations.mesh
			self.checkpoint_manager = function_configurations.checkpoint_manager
			self.initialize_state_function = function_configurations.initialize_state_function
		self.timer.log("configure functions and sharding them")

	@abstractmethod
	def create_collect_function(
		self,
		max_sequence_length: int,
		truncation_mode: Literal["keep_end", "keep_start"],
	) -> Callable:
		"""
		Creates a function to collect and process batches of data for training or evaluation.

		This function handles padding or truncating sequences to the specified `max_sequence_length`
		based on the chosen `truncation_mode`.

		Args:
		    max_sequence_length (int): The maximum allowed sequence length.
		    truncation_mode (typing.Literal["keep_end", "keep_start"], optional):
		        The truncation mode. Defaults to "keep_end".

		Returns:
		    Callable: A function that takes a batch of data and returns a processed batch.
		"""
		raise NotImplementedError

	@abstractmethod
	def configure_functions(self) -> TrainerConfigureFunctionOutput:
		"""
		Configures and JIT-compiles the training and evaluation step functions.

		This method sets up the necessary functions for training and evaluation, including:
		    - Initialization of the model state.
		    - Sharding of the model parameters and optimizer state.
		    - JIT-compilation of the training and evaluation step functions.

		Returns:
		    TrainerConfigureFunctionOutput: An object containing the configured functions and other relevant information.
		"""
		raise NotImplementedError

	def configure_dataloaders(self) -> TrainerConfigureDataloaderOutput:
		"""
		Configures the dataloaders for training and evaluation.

		This method creates the training and evaluation dataloaders using the provided
		datasets and data collator. It also determines the maximum number of training
		and evaluation steps based on the dataset sizes and training arguments.

		Returns:
		    TrainerConfigureDataloaderOutput: An object containing the configured dataloaders and the
		                                    maximum number of training and evaluation steps.
		"""

		def create_tf_dataset(
			dataset: "Dataset",  # noqa: F821 # type:ignore
			is_train: bool,  # noqa: F821 # type:ignore
		) -> Iterator[np.ndarray]:
			"""
			Creates a TensorFlow dataset from a Hugging Face Dataset.

			Args:
			    dataset (Dataset): The Hugging Face Dataset.
			    is_train (bool): Whether the dataset is for training.

			Returns:
			    Iterator[np.ndarray]: The TensorFlow dataset iterator.
			"""
			import tensorflow as tf

			return (
				dataset.to_tf_dataset(
					collate_fn=self.create_collect_function(
						max_sequence_length=self.arguments.max_sequence_length,
						truncation_mode=self.arguments.truncation_mode,
					),
					batch_size=self.arguments.total_batch_size,
					drop_remainder=True,
					shuffle=is_train,
					num_workers=self.arguments.dataloader_num_workers,
				)
				.repeat(self.arguments.num_train_epochs if is_train else 1)
				.prefetch(tf.data.AUTOTUNE)
				.as_numpy_iterator()
			)

		def create_tf_dataset_from_iterable(
			dataset: "IterableDataset",  # noqa: F821 # type:ignore
			is_train: bool,  # noqa: F821 # type:ignore
		) -> Iterator[np.ndarray]:
			"""
			Creates a TensorFlow dataset from an iterable Hugging Face Dataset.

			Args:
			    dataset (IterableDataset): The iterable Hugging Face Dataset.
			    is_train (bool): Whether the dataset is for training.

			Returns:
			    Iterator[np.ndarray]: The TensorFlow dataset iterator.
			"""
			import tensorflow as tf

			return (
				tf.data.Dataset.from_generator(
					lambda: dataset,
					output_signature={
						col: tf.TensorSpec(
							shape=(self.arguments.max_sequence_length,), dtype=tf.int32
						)
						for col in next(iter(dataset)).keys()
					},
				)
				.repeat(self.arguments.num_train_epochs if is_train else 1)
				.batch(self.arguments.total_batch_size, drop_remainder=False)
				.prefetch(tf.data.AUTOTUNE)
				.as_numpy_iterator()
			)

		def calculate_steps(
			dataset: Union["Dataset", "IterableDataset"],  # noqa: F821 # type:ignore
			is_train: bool,
		) -> int:
			"""
			Calculates the number of training or evaluation steps based on dataset length and arguments.

			Args:
			    dataset (Union[Dataset, IterableDataset]): The dataset to calculate steps for.
			    is_train (bool): Whether the dataset is for training.

			Returns:
			    int: The number of steps.

			Raises:
			    ValueError: If the dataset is a generator/streaming dataset and the number of steps is not specified.
			"""
			if hasattr(dataset, "__len__"):
				total_data_len = len(dataset)
				batch_size = (
					self.arguments.total_batch_size
					if is_train
					else self.arguments.eval_batch_size
				)
				num_steps = (
					(total_data_len + batch_size - 1)
					// batch_size
					* (self.arguments.num_train_epochs if is_train else 1)
				)
				max_steps = (
					self.arguments.max_training_steps
					if is_train
					else self.arguments.max_evaluation_steps
				)
				return min(num_steps, max_steps) if max_steps else num_steps
			else:
				num_steps = (
					self.arguments.max_training_steps
					if is_train
					else self.arguments.max_evaluation_steps
				)
				if not num_steps:
					raise ValueError(
						f"Specify the number of {'training' if is_train else 'evaluation'} steps for a generator/streaming dataset."
					)
				return num_steps

		def to_tf_dataloader(
			dataset: Union["Dataset", "IterableDataset"],  # noqa: F821 # type:ignore
			is_train: bool,
		) -> Iterator[np.ndarray]:
			"""
			Converts a Hugging Face Dataset to a TensorFlow dataloader.

			Args:
			    dataset (Union[Dataset, IterableDataset]): The Hugging Face Dataset.
			    is_train (bool): Whether the dataset is for training.

			Returns:
			    Iterator[np.ndarray]: The TensorFlow dataloader iterator.
			"""
			if hasattr(dataset, "__len__"):
				return create_tf_dataset(dataset, is_train)
			else:
				return create_tf_dataset_from_iterable(dataset, is_train)

		max_training_steps = calculate_steps(self.dataset_train, is_train=True)
		dataloader_train = to_tf_dataloader(self.dataset_train, is_train=True)

		if self.dataset_eval is not None and self.arguments.do_eval:
			max_evaluation_steps = calculate_steps(self.dataset_eval, is_train=False)
			dataloader_eval = to_tf_dataloader(self.dataset_eval, is_train=False)
		else:
			dataloader_eval, max_evaluation_steps = None, 0

		return TrainerConfigureDataloaderOutput(
			dataloader_train=dataloader_train,
			max_training_steps=max_training_steps,
			dataloader_eval=dataloader_eval,
			max_evaluation_steps=max_evaluation_steps,
		)

	def configure_model(self) -> TrainerConfigureModelOutput:
		"""
		Configures the model, optimizer, scheduler, and configuration.

		This method retrieves the model configuration from the model state, creates
		the optimizer and scheduler using the training arguments, and returns an
		object containing the configured model, optimizer, scheduler, and configuration.

		Returns:
		    TrainerConfigureModelOutput: An object containing the configured model, optimizer, scheduler, and configuration.
		"""
		extra_configs = self.arguments.extra_configs or {}
		assert self.arguments.model_class is not None, (
			"`model_class` is a required field "
			"please pass `model_class` to `TrainArguments`"
		)
		model = self._configure_custom_model(extra_configs)

		tx, scheduler = self.arguments.get_optimizer_and_scheduler(self.max_training_steps)
		if self.pruning_module is not None:
			tx = self.pruning_module.wrap_optax(tx)
		return TrainerConfigureModelOutput(
			model=model,
			tx=tx,
			scheduler=scheduler,
			config=getattr(model, "config", None),
		)

	def _configure_custom_model(self, extra_configs):
		"""
		Configures a custom model provided by the user.

		This method checks if a custom rule for partitioning is provided, sets the axis dimensions
		for the model configuration, and initializes the custom model class with the provided
		configurations.

		Args:
		    extra_configs (dict): Additional configurations to apply to the model.

		Returns:
		    EDPretrainedModel: The configured custom model.

		Raises:
		    AssertionError: If no custom rule is provided when initializing a custom model.
		"""
		if not hasattr(
			self.arguments.configs_to_initialize_model_class["config"],
			"get_partition_rules",
		):
			assert self.arguments.custom_rule is not None, (
				"If you are using a custom model to initialize, you must "
				"pass custom_rule for partition rules."
			)

		self.arguments.configs_to_initialize_model_class[
			"config"
		].axis_dims = self.arguments.sharding_array

		return self.arguments.model_class(
			**self.arguments.configs_to_initialize_model_class, _do_init=False
		)

	def _save_state(
		self,
		state: "EasyDeLState",  # noqa: F821 # type:ignore
		gather_fns: Optional[Any | Mapping[str, Callable] | dict[Callable]] = None,
		milestone: bool = False,
		save_dir: Optional[str] = None,
	) -> str:
		"""
		Saves the model state to a checkpoint file.

		This method handles generating the filename, managing the checkpoint limit, saving the
		state using the `EasyDeLState.save_state` method, and creating a README file with
		information about the trained model.

		Args:
		    state (EasyDeLState): The EasyDeLState object containing the model state to save.
		    gather_fns (Optional[Any | Mapping[str, Callable] | dict[Callable]], optional):
		        Functions for gathering sharded parameters. Defaults to None.
		    milestone (bool, optional): Whether this checkpoint is a milestone (e.g., end of epoch). Defaults to False.
		    save_dir (Optional[str], optional): The directory to save the checkpoint to. If None, defaults to the
		                                        directory specified in the training arguments. Defaults to None.

		Returns:
		    str: The filename of the saved checkpoint.
		"""
		step = self._get_current_step(state)
		checkpoint_dir = self._get_checkpoint_dir(save_dir)
		self._manage_checkpoint_limit(checkpoint_dir)

		filename = self._generate_checkpoint_filename(step, milestone)
		logger.info(f"Saving Model {filename}.")

		state.save_state(
			filename=filename,
			checkpoint_dir=checkpoint_dir,
			gather_fns=gather_fns,
			float_dtype=self.dtype,
			verbose=self.arguments.verbose,
			save_optimizer=self.arguments.save_optimizer_state,
		)

		self._save_readme(checkpoint_dir)
		return filename

	def _get_current_step(self, state):
		step = int(jax.device_get(state.step))
		if self.arguments.step_start_point is not None:
			step += self.arguments.step_start_point
		return step

	def _get_checkpoint_dir(self, save_dir):
		return (
			os.path.join(self.arguments.save_dir, self.arguments.model_name)
			if save_dir is None
			else save_dir
		)

	def _manage_checkpoint_limit(self, checkpoint_dir):
		if self.arguments.save_total_limit:
			checkpoint_files = glob(os.path.join(checkpoint_dir, "*.easy"))
			checkpoint_files.sort(key=os.path.getmtime)
			for old_checkpoint in checkpoint_files[: -self.arguments.save_total_limit]:
				os.remove(old_checkpoint)
				logger.info(
					f"Removed old checkpoint: {old_checkpoint}",
				)

	def _generate_checkpoint_filename(self, step, milestone):
		checkpoint_name = f"{self.arguments.model_name}-S{step}"
		filename = f"{checkpoint_name}_{step}" if milestone else checkpoint_name
		return f"{filename}.easy"

	def _save_readme(self, checkpoint_dir):
		with open(os.path.join(checkpoint_dir, "README.md"), "w") as f:
			f.write(self._get_information())

	@abstractmethod
	def train(self):
		"""Abstract method for training the model"""

	@abstractmethod
	def eval(self, state):
		"""Abstract method for evaluating the model"""

	def _get_information(self):
		partition_rules = pprint.pformat(
			self.arguments.custom_rule
			if self.arguments.custom_rule is not None
			else self.arguments.model_class.config_class.get_partition_rules(
				self.arguments.fully_sharded_data_parallel
			)
		)

		info = f"""
---
tags:
- EasyDeL
- {self.arguments.model_class.config_class.model_type}
---
# {self.arguments.model_name}

## Trained With [EasyDeL](https://github.com/erfanzar/EasyDeL)

EasyDeL is an open-source framework designed to enhance and streamline the training process of machine learning
models. With a primary focus on Jax, EasyDeL aims to provide convenient and effective solutions for 
training Flax/Jax models on TPU/GPU for both serving and training purposes.

## Using Example

### Using From EasyDeLState (_*.easy_ files)

```python
from easydel import EasyDeLState, AutoShardAndGatherFunctions
from jax import numpy as jnp, lax

shard_fns, gather_fns = AutoShardAndGatherFunctions.from_pretrained(
    "REPO_ID", # Pytorch State should be saved to in order to find shard gather fns with no effort, otherwise read docs.
    backend="gpu",
    depth_target=["params", "params"],
    flatten=False
)

state = EasyDeLState.load_state(
    "REPO_ID/{self.arguments.model_name}.easy",
    dtype=jnp.float16,
    param_dtype=jnp.float16,
    precision=lax.Precision("fastest"),
    verbose=True,
    state_shard_fns=shard_fns
)
# State file Ready to use ...
```

### Using From AutoEasyDeLModelForCausalLM (_from PyTorch_)

```python
from easydel import AutoEasyDeLModelForCausalLM
from jax import numpy as jnp, lax

model, params = AutoEasyDeLModelForCausalLM.from_pretrained(
    "REPO_ID/{self.arguments.model_name}",
    dtype=jnp.float16,
    param_dtype=jnp.float16,
    precision=lax.Precision("fastest"),
    auto_shard_params=True,
)
# Model and Parameters Ready to use ...
```

### Using From AutoEasyDeLModelForCausalLM (_from EasyDeL_)

```python
from easydel import AutoEasyDeLModelForCausalLM
from jax import numpy as jnp, lax

model, params = AutoEasyDeLModelForCausalLM.from_pretrained(
    "REPO_ID/{self.arguments.model_name}",
    dtype=jnp.float16,
    param_dtype=jnp.float16,
    precision=lax.Precision("fastest"),
    auto_shard_params=True,
    from_torch=False
)
# Model and Parameters Ready to use ...
```

## Training Details

- Model Architecture : {self.arguments.model_class.config_class.model_type}
- Platform : {jax.devices()[0].platform.upper()}
- Number of Devices : {len(jax.devices())}
- Learning Rate Start : {self.arguments.learning_rate}
- Learning Rate End : {self.arguments.learning_rate_end}
- Optimizer : {self.arguments.optimizer}
- Scheduler : {self.arguments.scheduler}
- Warmup Steps : {self.arguments.warmup_steps}
- Weight Decay : {self.arguments.weight_decay}
- Z Loss : {self.arguments.z_loss}
- Epoch : {self.arguments.num_train_epochs}
- Batch size : {self.arguments.total_batch_size}
- Sequence Length : {self.arguments.max_sequence_length}
- EasyDeL init InputShape : {self.arguments.init_input_shape}
- Dtype : {self.arguments.dtype}
- Params Dtype : {self.arguments.param_dtype}
- Gradient checkpointing : {self.arguments.gradient_checkpointing}
- Fully Sharded Data Parallel : {self.arguments.fully_sharded_data_parallel}
- Force batch GradientAccumulation : {self.arguments.force_batch_and_gradient_accumulation_steps_calculation}
- Gradient Accumulation Steps : {self.arguments.gradient_accumulation_steps}
- Max Training Steps : {self.arguments.max_training_steps}
- Max Evaluation Steps : {self.arguments.max_evaluation_steps}
- Training Time : {self.arguments.training_time}

#### Sharding Partition Rules
```python
partition_rules = {partition_rules}
```
        """
		return info

	def save_pretrained(
		self,
		state: "EasyDeLState",  # noqa: F821 # type:ignore
		save_dir: Optional[str] = None,
		gather_fns: Optional[Any | Mapping[str, Callable] | dict[Callable]] = None,
		to_torch: bool = False,
		base_hf_auto_class=None,
		easystate_to_huggingface_model_kwargs: Optional[dict] = None,
		add_params_field_to_torch_convertation: bool = False,
		torch_save_pretrained_kwargs: Optional[dict] = None,
	):
		save_dir = save_dir or os.path.join(
			self.arguments.save_dir, self.arguments.model_name
		)

		if base_hf_auto_class is None:
			from transformers import AutoModelForCausalLM as base_hf_auto_class
		if to_torch:
			return self._save_to_torch(
				state,
				save_dir,
				base_hf_auto_class,
				easystate_to_huggingface_model_kwargs,
				torch_save_pretrained_kwargs,
			)
		else:
			return self._save_state(state=state, gather_fns=gather_fns, save_dir=save_dir)

	def _save_to_torch(
		self,
		state,
		save_dir,
		base_hf_auto_class,
		easystate_to_huggingface_model_kwargs,
		torch_save_pretrained_kwargs,
	):
		from easydel.transform.parameters_transformation import (
			easystate_to_huggingface_model,
		)

		easystate_to_huggingface_model_kwargs = easystate_to_huggingface_model_kwargs or {}
		torch_save_pretrained_kwargs = torch_save_pretrained_kwargs or {}

		model_config = state.module_config or state.module.config_class
		model_type = model_config.model_type
		model_class = base_hf_auto_class._model_mapping[type(model_config)]

		hf_model_config = self._create_hf_model_config(state, model_config, model_type)

		hf_model = easystate_to_huggingface_model(
			state=state,
			base_huggingface_module=model_class,
			config=hf_model_config,
			**easystate_to_huggingface_model_kwargs,
		)

		self._save_readme(save_dir)
		hf_model.save_pretrained(save_dir, **torch_save_pretrained_kwargs)
		return hf_model

	def _create_hf_model_config(
		self,
		state: "EasyDeLState",  # noqa: F821 # type:ignore
		model_config,
		model_type,
	):
		from transformers import AutoConfig

		hf_model_config = AutoConfig.for_model(model_type=model_type)
		unsafe_dict = state.unsafe_dict(model_config.__dict__)
		blocked_statics = ["torch_dtype"]

		for k, v in unsafe_dict.items():
			if (
				not k.startswith("_")
				and k in hf_model_config.__dict__
				and k not in blocked_statics
			):
				if isinstance(v, str) and v.isnumeric():
					v = int(float(v)) if float(v).is_integer() else float(v)
				setattr(hf_model_config, k, v)

		return hf_model_config

	def specs_to_name_sharding(self, tree, mesh=None):
		mesh = mesh or self.mesh or self.arguments.get_mesh()
		return jax.tree_util.tree_map(
			lambda spec: jax.sharding.NamedSharding(spec=spec, mesh=mesh),
			tree,
		)

	def calculate_number_total_flops_per_device(self, params):
		return (
			6
			* sum(x.size for x in jax.tree_util.tree_flatten(unfreeze(params))[0])
			* (self.arguments.total_batch_size * self.arguments.max_sequence_length)
		) / jax.device_count()
