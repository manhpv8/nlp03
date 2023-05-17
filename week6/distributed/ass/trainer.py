import time
from typing import Union
import os
import torch
from torch.utils.data import DataLoader
from tqdm import tqdm
from torch.nn.parallel import DistributedDataParallel as DDP
import torch.nn.functional as F
import torch.multiprocessing as mp
from torch.distributed import init_process_group, destroy_process_group
from utils import Prompter
from datasets import load_dataset
from peft import LoraConfig, get_peft_model, prepare_model_for_int8_training
from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer
from utils import download_from_driver
from transformers import (
    AutoConfig, 
    AutoModelForCausalLM, 
    AutoTokenizer, 
    default_data_collator
  )

from accelerate import Accelerator
from torch.utils.data.distributed import DistributedSampler


model_path = 'bigscience/bloom-560m'
data_path = 'alpaca_data.json'
output_dir = 'checkpoints/'
size_valid_set = 0.1
max_length = 256
num_epochs = 30
batch_size = 8
gradient_accumulation_steps = 8


learning_rate = 1e-5
lr_scheduler_type = 'cosine'
num_warmup_steps = 100
weight_decay = 0.06

local_rank = 0
use_bf16 = False
seed = 0
log_freq = 1
eval_freq = 150

class Trainer:
    def __init__(
            self,
            model, 
            num_epochs,
            max_length,
            batch_size,
            gpu_id
            
            ):
        
        
        self.num_epochs = num_epochs
        self.max_length = max_length
        self.batch_size = batch_size
        self.gpu_id = gpu_id

        self.model = model.to(f"cuda:{self.gpu_id}")
        # setup the optimizer
        self.optimizer = torch.optim.AdamW(self.model.parameters(), lr=learning_rate)
        self.model = DDP(self.model, device_ids=[self.gpu_id], output_device=self.gpu_id)

    def _run_batch(self,batch):
        self.optimizer.zero_grad()
        outputs = self.model(**batch) 
        loss = outputs.loss
        loss.backward()
        self.optimizer.step()

        return loss.item()

    def _run_epoch(self,train_loader, epoch):
        epoch_loss = 0
        train_loader.sampler.set_epoch(epoch)
        for step, batch in enumerate(tqdm(train_loader)):
            loss = self._run_batch(batch)
            epoch_loss += loss

        return epoch_loss

    def run(self, data_trainloader, eval_dataset):
        avg_loss = 0
        for epoch in range(self.num_epochs):
            self.model.train()
            epoch_loss = self._run_epoch(data_trainloader, epoch)
            avg_loss += epoch_loss
            print(f"epoch {epoch + 1} | train loss = {epoch_loss}")

        print(f"total epoch: {self.num_epochs} | avg train loss = {avg_loss/self.num_epochs}")

            # TODO
            # evaluate
            # eval_loader = DataLoader(
            #     eval_dataset,
            #     batch_size = self.batch_size,
            #     collate_fn=default_data_collator)


def create_datasets(tokenizer, max_length, gpu_id):
    def tokenize(prompt, add_eos_token=True):
        result = tokenizer(
            prompt,
            truncation=True,
            max_length=max_length,
            padding="max_length"
            )

        if (
            result["input_ids"][-1] != tokenizer.eos_token_id
            and len(result["input_ids"]) < max_length
            and add_eos_token
        ):
            
            result["input_ids"].append(tokenizer.eos_token_id)
            result["attention_mask"].append(1)

        result["labels"] = result["input_ids"].copy()
        return result
    
    def generate_and_tokenize_prompt(data_point):
        full_prompt = prompter.generate_prompt(
            data_point["instruction"],
            data_point["input"],
            data_point["output"],
        )
        tokenized_full_prompt = tokenize(full_prompt)

        # Chuyển đổi thiết bị của các tensors trong `tokenized_full_prompt`
        for key, value in tokenized_full_prompt.items():
            if isinstance(value, torch.Tensor):
                tokenized_full_prompt[key] = value.to(gpu_id)

        return tokenized_full_prompt
    
    prompter = Prompter()
    dataset = load_dataset('json', split='train', data_files=data_path)
    dataset = dataset.train_test_split(test_size=size_valid_set, seed=seed)

    train_data = dataset["train"].shuffle().map(generate_and_tokenize_prompt)
    valid_data = dataset["test"].map(generate_and_tokenize_prompt)
    train_data.set_format("torch")
    
    
    dataset["test"].to_json('dataset/val_data.json')
    print(f"Size of the train set: {len(train_data)}. Size of the validation set: {len(valid_data)}")
    
    return train_data, valid_data



def ddp_setup():
    init_process_group(backend="nccl")
    
def load_tokenizer_from_pretrained_model(model_path):
    print('Start config')
    config = AutoConfig.from_pretrained(model_path)
    architecture = config.architectures[0]
    print('Start tokenizer')
    tokenizer = AutoTokenizer.from_pretrained(model_path)
    print('End tokenizer')

    if "Llama" in architecture:
        print("Setting EOS, BOS, UNK, and PAD tokens for LLama tokenizer")
        tokenizer.add_special_tokens(
            {
                "eos_token": "</s>",
                "bos_token": "</s>",
                "unk_token": "</s>",
            }
        )
        tokenizer.pad_token_id = (
            0  # unk. we want this to be different from the eos token
        )
    
    return tokenizer

def load_pretrained_model():
    model = AutoModelForCausalLM.from_pretrained(
        model_path,
        trust_remote_code=True,
        load_in_8bit=True,
        torch_dtype=torch.float16,
        device_map={"": Accelerator().process_index},
    )
    model = prepare_model_for_int8_training(model)

    lora_config = LoraConfig(
        r=16,
        lora_alpha=32,
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
    )
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()

    return model


if __name__ == "__main__":
    ddp_setup()
    torch.cuda.empty_cache()

    # Download data
    data_driver_path = 'https://drive.google.com/file/d/1TIdshkGnECTS1ADX39dXcevQDIqFCNtz/view?usp=sharing'
    download_from_driver(data_driver_path= data_driver_path, location_path= data_path)

    local_rank =  int(os.environ["LOCAL_RANK"])
    # Get tokenizer
    tokenizer = load_tokenizer_from_pretrained_model(model_path = model_path)
    # Prepare dataset
    train_dataset, eval_dataset = create_datasets(
        tokenizer = tokenizer, 
        max_length=max_length,
        gpu_id = local_rank)
    
    # Prepare model
    model = load_pretrained_model()
    
    # Create the DataLoaders
    data_trainloader = DataLoader(
        train_dataset,
        batch_size = batch_size,
        sampler=ẻ(train_dataset),
        collate_fn=lambda x: {
            "input_ids": torch.stack([sample["input_ids"].to(local_rank) for sample in x]),
            "attention_mask": torch.stack([sample["attention_mask"].to(local_rank) for sample in x]),
            "labels": torch.stack([sample["labels"].to(local_rank) for sample in x]),
        })
    
    # prepare trainer
    trainer = Trainer(
        model = model, 
        num_epochs = num_epochs,
        max_length = max_length,
        batch_size = batch_size,
        gpu_id=local_rank,
        data_trainloader = data_trainloader
        )
    
    # execute trainer 
    trainer.run(
        eval_dataset=eval_dataset
    )

    destroy_process_group()