import torch
import datasets
from transformers import AutoTokenizer
from tqdm import tqdm
from collate import SegmentCollator, DynamicBatchSampler


dataset = datasets.load_from_disk('/home/nlp/shon711/lingmess-coref/data/dataset')
tokenizer = AutoTokenizer.from_pretrained('roberta-base', cache_dir='cache')
device = torch.device('cpu')

collator = SegmentCollator(tokenizer=tokenizer, device=device, max_segment_len=512)
sampler = DynamicBatchSampler(
    dataset['train'],
    collator=collator,
    max_tokens=5000,
    max_segment_len=512,
    max_doc_len=None
)

total_batches_dynamic = 0
total_leftover_batches_dynamic = 0
total_tokens_dynamic = 0
padding_tokens_dynamic = 0
batch_lengths_dynamic = []
for batch in tqdm(sampler):
    total_batches_dynamic += 1

    input_ids = batch['input_ids']
    total_tokens_dynamic += input_ids.numel()
    padding_tokens_dynamic += input_ids[input_ids == tokenizer.pad_token_id].numel()

    if 'leftovers' in batch and len(batch['leftovers']['input_ids']) > 0:
        total_leftover_batches_dynamic += 1
        input_ids = batch['leftovers']['input_ids']
        total_tokens_dynamic += input_ids.numel()
        padding_tokens_dynamic += input_ids[input_ids == tokenizer.pad_token_id].numel()

print(f"Total Examples   : {len(sampler.dataset)}") # Seeing the tqdm stats.
print(f"Total Batches    : {total_batches_dynamic}") # Seeing the tqdm stats.
print(f"Total Leftovers  : {total_leftover_batches_dynamic}") # Seeing the tqdm stats.
print(f"Padding Tokens   : {padding_tokens_dynamic}")
print(f"Input Tokens     : {total_tokens_dynamic - padding_tokens_dynamic}")
print(f"Total Tokens     : {total_tokens_dynamic}")
print(f"Padding Tokens % : {(padding_tokens_dynamic*100)/total_tokens_dynamic}")
print('--------------------')
print()
