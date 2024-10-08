from easydel.trainers.utils import (
	create_constant_length_dataset,
	conversations_formatting_function,
)
from datasets import load_dataset
from transformers import AutoTokenizer


def to_role_and_content(field):
	return {
		"conversation": [
			{"role": "user", "content": field["conversation"][0]["input"]},
			{"role": "assistant", "content": field["conversation"][0]["output"]},
		]
	}


def create_prompt_creator(tokenizer):
	def _pc(sample):
		return conversations_formatting_function(tokenizer, messages_field="conversation")(
			to_role_and_content(sample)
		)

	return _pc


def main():
	dove = load_dataset(
		"LDJnr/Pure-Dove",
	)
	tokenizer = AutoTokenizer.from_pretrained("mistralai/Mistral-7B-Instruct-v0.2")

	prompt_creator = create_prompt_creator(tokenizer)
	dts = create_constant_length_dataset(
		tokenizer=tokenizer,
		dataset=dove["train"],
		dataset_text_field="conversation",
		formatting_func=prompt_creator,
	)
	for k in dts():
		print(k)
		break


if __name__ == "__main__":
	main()
