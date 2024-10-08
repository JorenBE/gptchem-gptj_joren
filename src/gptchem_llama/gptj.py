"""Based on https://github.com/gustavecortal/gpt-j-fine-tuning-example/blob/main/finetune_8bit_models.ipynb"""
import gc
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Optional, Union

import pandas as pd
import torch
import torch.nn.functional as F
import transformers
from bitsandbytes.functional import dequantize_blockwise, quantize_blockwise
from bitsandbytes.optim import Adam8bit
from datasets import Dataset, load_dataset
from torch import nn
from torch.cuda.amp import custom_bwd, custom_fwd
from torch.utils.data import DataLoader
from tqdm.auto import tqdm


class DequantizeAndLinear(torch.autograd.Function):
    """Blockwise dequantization of weights and linear layer."""

    @staticmethod
    @custom_fwd
    def forward(
        ctx,
        input: torch.Tensor,
        weights_quantized: torch.ByteTensor,
        absmax: torch.FloatTensor,
        code: torch.FloatTensor,
        bias: torch.FloatTensor,
    ):
        weights_deq = dequantize_blockwise(weights_quantized, absmax=absmax, code=code)
        ctx.save_for_backward(input, weights_quantized, absmax, code)
        ctx._has_bias = bias is not None
        return F.linear(input, weights_deq, bias).clone()

    @staticmethod
    @custom_bwd
    def backward(ctx, grad_output: torch.Tensor):
        assert (
            not ctx.needs_input_grad[1]
            and not ctx.needs_input_grad[2]
            and not ctx.needs_input_grad[3]
        )
        input, weights_quantized, absmax, code = ctx.saved_tensors
        # grad_output: [*batch, out_features]
        weights_deq = dequantize_blockwise(weights_quantized, absmax=absmax, code=code)
        grad_input = grad_output @ weights_deq
        grad_bias = grad_output.flatten(0, -2).sum(dim=0) if ctx._has_bias else None
        return grad_input, None, None, None, grad_bias


def quantize_blockise_lowmemory(matrix: torch.Tensor, chunk_size: int = 2**20):
    """Quantize a matrix in chunks to reduce memory usage."""
    assert chunk_size % 4096 == 0
    code = None
    chunks = []
    absmaxes = []
    flat_tensor = matrix.view(-1)
    for i in range((matrix.numel() - 1) // chunk_size + 1):
        input_chunk = flat_tensor[i * chunk_size : (i + 1) * chunk_size].clone()
        quantized_chunk, (absmax_chunk, code) = quantize_blockwise(input_chunk, code=code)
        chunks.append(quantized_chunk)
        absmaxes.append(absmax_chunk)

    matrix_i8 = torch.cat(chunks).reshape_as(matrix)
    absmax = torch.cat(absmaxes)
    return matrix_i8, (absmax, code)


class FrozenBNBLinear(nn.Module):
    """Linear layer quantized with bits and bytes."""

    def __init__(self, weight, absmax, code, bias=None):
        assert isinstance(bias, nn.Parameter) or bias is None
        super().__init__()
        self.out_features, self.in_features = weight.shape
        self.register_buffer("weight", weight.requires_grad_(False))
        self.register_buffer("absmax", absmax.requires_grad_(False))
        self.register_buffer("code", code.requires_grad_(False))
        self.adapter = None
        self.bias = bias

    def forward(self, input):
        output = DequantizeAndLinear.apply(input, self.weight, self.absmax, self.code, self.bias)
        if self.adapter:
            output += self.adapter(input)
        return output

    @classmethod
    def from_linear(cls, linear: nn.Linear) -> "FrozenBNBLinear":
        weights_int8, state = quantize_blockise_lowmemory(linear.weight)
        return cls(weights_int8, *state, linear.bias)

    def __repr__(self):
        return f"{self.__class__.__name__}({self.in_features}, {self.out_features})"


class FrozenBNBEmbedding(nn.Module):
    """Embedding layer quantized with bits and bytes."""

    def __init__(self, weight, absmax, code):
        super().__init__()
        self.num_embeddings, self.embedding_dim = weight.shape
        self.register_buffer("weight", weight.requires_grad_(False))
        self.register_buffer("absmax", absmax.requires_grad_(False))
        self.register_buffer("code", code.requires_grad_(False))
        self.adapter = None

    def forward(self, input, **kwargs):
        with torch.no_grad():
            # note: both quantuized weights and input indices are *not* differentiable
            weight_deq = dequantize_blockwise(self.weight, absmax=self.absmax, code=self.code)
            output = F.embedding(input, weight_deq, **kwargs)
        if self.adapter:
            output += self.adapter(input)
        return output

    @classmethod
    def from_embedding(cls, embedding: nn.Embedding) -> "FrozenBNBEmbedding":
        weights_int8, state = quantize_blockise_lowmemory(embedding.weight)
        return cls(weights_int8, *state)

    def __repr__(self):
        return f"{self.__class__.__name__}({self.num_embeddings}, {self.embedding_dim})"


def convert_to_int8(model):
    """Convert linear and embedding modules to 8-bit with optional adapters"""
    for module in list(model.modules()):
        for name, child in module.named_children():
            if isinstance(child, nn.Linear):
                print(name, child)
                setattr(
                    module,
                    name,
                    FrozenBNBLinear(
                        weight=torch.zeros(
                            child.out_features, child.in_features, dtype=torch.uint8
                        ),
                        absmax=torch.zeros((child.weight.numel() - 1) // 4096 + 1),
                        code=torch.zeros(256),
                        bias=child.bias,
                    ),
                )
            elif isinstance(child, nn.Embedding):
                setattr(
                    module,
                    name,
                    FrozenBNBEmbedding(
                        weight=torch.zeros(
                            child.num_embeddings, child.embedding_dim, dtype=torch.uint8
                        ),
                        absmax=torch.zeros((child.weight.numel() - 1) // 4096 + 1),
                        code=torch.zeros(256),
                    ),
                )


class GPTJBlock(transformers.models.gptj.modeling_gptj.GPTJBlock):
    def __init__(self, config):
        super().__init__(config)

        convert_to_int8(self.attn)
        convert_to_int8(self.mlp)


class GPTJModel(transformers.models.gptj.modeling_gptj.GPTJModel):
    def __init__(self, config):
        super().__init__(config)
        convert_to_int8(self)


class GPTJForCausalLM(transformers.models.gptj.modeling_gptj.GPTJForCausalLM):
    def __init__(self, config):
        super().__init__(config)
        convert_to_int8(self)


transformers.models.gptj.modeling_gptj.GPTJBlock = GPTJBlock


config = transformers.GPTJConfig.from_pretrained("EleutherAI/gpt-j-6B")
config.pad_token_id = config.eos_token_id

tokenizer = transformers.AutoTokenizer.from_pretrained("EleutherAI/gpt-j-6B")
tokenizer.pad_token = config.pad_token_id
tokenizer.padding_side = "left"
tokenizer.pad_token = tokenizer.eos_token


def add_adapters(model, adapter_dim=4, p=0.1):
    """http://arxiv.org/abs/2106.09685"""
    assert adapter_dim > 0

    for name, module in model.named_modules():
        if isinstance(module, FrozenBNBLinear):
            if "attn" in name or "mlp" in name or "head" in name:
                print("Adding adapter to", name)
                module.adapter = nn.Sequential(
                    nn.Linear(module.in_features, adapter_dim, bias=False),
                    nn.Dropout(p=p),
                    nn.Linear(adapter_dim, module.out_features, bias=False),
                )
                print("Initializing", name)
                nn.init.zeros_(module.adapter[2].weight)

            else:
                print("Not adding adapter to", name)
        elif isinstance(module, FrozenBNBEmbedding):
            print("Adding adapter to", name)
            module.adapter = nn.Sequential(
                nn.Embedding(module.num_embeddings, adapter_dim),
                nn.Dropout(p=p),
                nn.Linear(adapter_dim, module.embedding_dim, bias=False),
            )
            print("Initializing", name)
            nn.init.zeros_(module.adapter[2].weight)


def load_model(path="hivemind/gpt-j-6B-8bit", adapter_dim=4, p=0.1, device=None):
    gpt = GPTJForCausalLM.from_pretrained(path, low_cpu_mem_usage=True)
    add_adapters(gpt, adapter_dim=adapter_dim, p=p)
    if device is None:
        device = "cuda" if torch.cuda.is_available() else "cpu"
    gpt.to(device)

    return gpt


def tokenize_function(examples, max_length=128):
    return tokenizer(examples["sentence"], padding=True, truncation=True, max_length=max_length)


def create_datasets(
    train_df: pd.DataFrame,
    test_df: pd.DataFrame,
    prompt_column: str,
    completion_column: str,
    folder: Union[str, Path],
):
    train_df["sentence"] = train_df.apply(
        lambda row: row[prompt_column] + row[completion_column], axis=1
    )
    test_df["sentence"] = test_df.apply(
        lambda row: row[prompt_column] + row[completion_column], axis=1
    )

    train_file = Path(folder) / "train.csv"
    test_file = Path(folder) / "test.csv"

    train_df.to_csv(train_file, index=False)
    test_df.to_csv(test_file, index=False)

    dataset = load_dataset("csv", data_files={"train": str(train_file), "test": str(test_file)})
    return dataset


def tokenize_dataset(datasets, remove_columns=None, max_length=128):
    if remove_columns is None:
        remove_columns = ["sentence"]

    curried_tokenize_function = partial(tokenize_function, max_length=max_length)

    tokenized_datasets = datasets.map(curried_tokenize_function, batched=True)
    tokenized_datasets = tokenized_datasets.remove_columns(remove_columns)
    tokenized_datasets.set_format("torch")
    return tokenized_datasets


def create_dataloaders_from_frames(train_df, test_df: Optional[pd.DataFrame], batch_size=8):
    ds = {
        "train": GPTJDataset(train_df, tokenizer, max_length=1024)
        if train_df is not None
        else None,
        "test": GPTJDataset(test_df, tokenizer, max_length=1024) if test_df is not None else None,
    }
    train_dataloader = DataLoader(ds["train"], shuffle=True, batch_size=batch_size)
    test_dataloader = (
        DataLoader(ds["test"], shuffle=False, batch_size=batch_size)
        if test_df is not None
        else None
    )
    return {"train": train_dataloader, "test": test_dataloader}


class GPTJDataset(Dataset):
    def __init__(self, dataframe, tokenizer, max_length=1024):
        texts = []
        completion_lens = []
        for i, row in dataframe.iterrows():
            t = "".join([row["prompt"], row["completion"]])
            texts.append(t)
            l = len(tokenizer.tokenize(row["completion"]))
            completion_lens.append(l)

        tokens = tokenizer(
            texts, truncation=True, padding=True, max_length=max_length, return_tensors="pt"
        )
        self.input_ids = tokens["input_ids"]
        self.attention_mask = tokens["attention_mask"]
        self.labels = []
        for i in range(len(self.input_ids)):
            b_labels = self.input_ids[i].clone()
            b_labels[: -completion_lens[i]] = -100
            self.labels.append(b_labels)

        self.labels = torch.stack(self.labels)

    def __len__(self):
        return len(self.labels)

    def __getitem__(self, idx):
        return {
            "input_ids": self.input_ids[idx],
            "attention_mask": self.attention_mask[idx],
            "labels": self.labels[idx],
        }


def train(
    model,
    train_dataloader,
    lr: float = 1e-5,
    weight_decay: float = 0.01,
    num_epochs: int = 5,
    lr_decay: float = 0.1,
    filename: Optional[str] = None,
    gc_optimizer: bool = True,
):
    device = model.device
    print(device)
    if filename is None:
        time_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"model_{time_str}.pt"

    model.gradient_checkpointing_enable()
    optimizer = Adam8bit(model.parameters(), lr=lr, weight_decay=weight_decay)
    num_training_steps = num_epochs * len(train_dataloader)

    lr_scheduler = transformers.get_linear_schedule_with_warmup(
        optimizer, int(num_training_steps * lr_decay), num_training_steps
    )

    scaler = torch.cuda.amp.GradScaler()
    progress_bar = tqdm(range(num_training_steps))
    model.train()
    model.gradient_checkpointing_enable()
    k = 0

    for epoch in range(num_epochs):
        for batch in train_dataloader:
            k = k + 1
            if k % 500 == 0:
                print(k)
                state = {
                    "k": k,
                    "epoch": num_epochs,
                    "lr_scheduler": lr_scheduler.state_dict(),
                    "state_dict": model.state_dict(),
                    "optimizer": optimizer.state_dict(),
                }
                # torch.save(state, filename)

            batch = {k: v.to(device) for k, v in batch.items()}

            optimizer.zero_grad()

            with torch.cuda.amp.autocast():
                out = model.forward(
                    **batch,
                )

                loss = F.cross_entropy(
                    out.logits[:, :-1, :].flatten(0, -2),
                    batch["input_ids"][:, 1:].flatten(),
                    reduction="mean",
                    label_smoothing=0.1,
                )

            print(loss)

            scaler.scale(loss).backward()
            scaler.unscale_(optimizer)
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            scaler.step(optimizer)
            scaler.update()

            lr_scheduler.step()
            progress_bar.update(1)

    if gc_optimizer:
        model.eval()
        optimizer = None
        lr_scheduler = None
        scaler = None
        loss = None
        out = None
        batch = None
        del loss
        del batch
        del out
        del optimizer
        del lr_scheduler
        del scaler

        gc.collect()
        torch.cuda.empty_cache()


def complete(
    model,
    prompt_text: str,
    max_length: int = 128,
    top_k: int = 50,
    top_p: float = 0.9,
    temperature: float = 1.0,
    do_sample: bool = True,
    repetition_penalty: float = 1.2,
    num_beams: int = 1,
):
    model.eval()
    device = model.device
    with torch.no_grad():
        prompt = tokenizer(
            prompt_text, truncation=True, padding=True, max_length=max_length, return_tensors="pt"
        )
        prompt = {key: value.to(device) for key, value in prompt.items()}
        out = model.generate(
            **prompt,
            max_length=max_length,
            top_k=top_k,
            top_p=top_p,
            temperature=temperature,
            do_sample=do_sample,
            repetition_penalty=repetition_penalty,
            num_beams=num_beams,
        )
        return {"out": out, "decoded": tokenizer.decode(out[0])}
