"""
Fine-tune Whisper on your voice. Needs an NVIDIA GPU — run it on Google Colab (free T4 is
enough for small.en), not on this PC. Standalone: it doesn't import anything from Jarvis.

On Colab (Runtime > Change runtime type > T4 GPU), upload this file and the zip from
export.py, then:

    !pip install -q transformers peft ctranslate2 soundfile
    !python finetune.py --data jarvis_voice_data.zip

It trains a LoRA adapter on openai/whisper-small.en (only ~1% of the weights change, so a few
hundred recordings can't wreck what it already knows), merges it, converts the result to
faster-whisper's format and writes whisper-me.zip. Download that, unzip it into
jarvis/models/whisper-me, then compare and switch:

    python scripts/voice/benchmark.py small.en models/whisper-me
    # core/config.py:  WHISPER_MODEL = os.path.join(ROOT, "models", "whisper-me")

Options: --base openai/whisper-base.en (faster, less accurate), --epochs, --lr.
"""

import argparse
import json
import os
import random
import shutil
import zipfile

import numpy as np
import soundfile as sf
import torch


def load(data_zip, folder="voice_data"):
    with zipfile.ZipFile(data_zip) as z:
        z.extractall(folder)
    rows = [json.loads(l) for l in open(os.path.join(folder, "metadata.jsonl"), encoding="utf-8")]
    prompt_path = os.path.join(folder, "prompt.txt")
    prompt = open(prompt_path, encoding="utf-8").read().strip() if os.path.exists(prompt_path) else ""
    for r in rows:
        audio, rate = sf.read(os.path.join(folder, r["audio"]), dtype="float32")
        assert rate == 16000, f"{r['audio']}: expected 16 kHz"
        r["array"] = audio
    train = [r for r in rows if r["split"] == "train"]
    test = [r for r in rows if r["split"] == "test"] or train[-max(1, len(train) // 10):]
    return train, test, prompt


def word_errors(ref, hyp):
    import re
    norm = lambda t: re.sub(r"\s+", " ", re.sub(r"[^\w\s']", " ", t.lower()).replace("'", "")).split()
    r, h = norm(ref), norm(hyp)
    row = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, row[0] = row[0], i
        for j in range(1, len(h) + 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
    return row[len(h)], len(r)


def evaluate(model, processor, rows, device, dtype):
    model.eval()
    errors = words = 0
    with torch.no_grad():
        for i in range(0, len(rows), 16):
            batch = rows[i:i + 16]
            feats = processor.feature_extractor([r["array"] for r in batch], sampling_rate=16000,
                                                return_tensors="pt").input_features.to(device, dtype)
            out = model.generate(input_features=feats, max_new_tokens=100)
            for r, text in zip(batch, processor.batch_decode(out, skip_special_tokens=True)):
                e, n = word_errors(r["text"], text)
                errors += e
                words += n
    return errors / max(words, 1)


def make_batch(rows, processor, prompt_ids, use_prompt, device, dtype):
    """
    Decoder sequence: [<|startofprev|> prompt…] <|startoftranscript|> <|notimestamps|> text <|endoftext|>.
    Loss only on the text and the end token — never on the prompt — which is how Jarvis
    decodes (faster-whisper puts the same prompt in front).
    """
    feats = processor.feature_extractor([r["array"] for r in rows], sampling_rate=16000,
                                        return_tensors="pt").input_features.to(device, dtype)
    seqs, masks = [], []
    for r, with_prompt in zip(rows, use_prompt):
        target = processor.tokenizer(" " + r["text"].strip()).input_ids   # sot, notimestamps, text, eot
        prefix = prompt_ids if with_prompt else []
        seqs.append(prefix + target)
        masks.append(len(prefix) + 1)       # first label position that counts (after <|startoftranscript|>)
    width = max(len(s) for s in seqs) - 1
    pad = processor.tokenizer.pad_token_id if processor.tokenizer.pad_token_id is not None else processor.tokenizer.eos_token_id
    inputs = torch.full((len(seqs), width), pad, dtype=torch.long)
    labels = torch.full((len(seqs), width), -100, dtype=torch.long)
    for i, (s, start) in enumerate(zip(seqs, masks)):
        inputs[i, :len(s) - 1] = torch.tensor(s[:-1])
        for j in range(start - 1, len(s) - 1):
            labels[i, j] = s[j + 1]
    return feats, inputs.to(device), labels.to(device)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="jarvis_voice_data.zip")
    ap.add_argument("--base", default="openai/whisper-small.en")
    ap.add_argument("--out", default="whisper-me")
    ap.add_argument("--epochs", type=int, default=5)
    ap.add_argument("--lr", type=float, default=5e-4)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--rank", type=int, default=32)
    ap.add_argument("--cpu", action="store_true", help="smoke test only — far too slow for real training")
    args = ap.parse_args()

    from peft import LoraConfig, get_peft_model
    from transformers import WhisperForConditionalGeneration, WhisperProcessor

    device = "cpu" if args.cpu or not torch.cuda.is_available() else "cuda"
    if device == "cpu" and not args.cpu:
        raise SystemExit("No GPU found. On Colab: Runtime > Change runtime type > T4 GPU.")
    random.seed(0)
    torch.manual_seed(0)

    train, test, prompt = load(args.data)
    print(f"{len(train)} training / {len(test)} test recordings")
    processor = WhisperProcessor.from_pretrained(args.base)
    model = WhisperForConditionalGeneration.from_pretrained(args.base).to(device)
    model.generation_config.forced_decoder_ids = None
    prompt_ids = list(processor.get_prompt_ids(prompt)) if prompt else []

    before = evaluate(model, processor, test, device, torch.float32)
    print(f"WER before: {before:.1%}")

    lora = LoraConfig(r=args.rank, lora_alpha=args.rank * 2, lora_dropout=0.05,
                      target_modules=["q_proj", "k_proj", "v_proj", "out_proj"])
    model = get_peft_model(model, lora)
    model.print_trainable_parameters()
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=args.lr, weight_decay=0.01)
    steps = args.epochs * ((len(train) + args.batch - 1) // args.batch)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=args.lr, total_steps=max(steps, 1), pct_start=0.1)
    use_amp = device == "cuda"
    scaler = torch.amp.GradScaler("cuda", enabled=use_amp)

    for epoch in range(args.epochs):
        model.train()
        random.shuffle(train)
        total = 0.0
        for i in range(0, len(train), args.batch):
            rows = train[i:i + args.batch]
            # Half with the prompt, half without: robust whether or not the vocabulary prompt is on
            feats, inputs, labels = make_batch(rows, processor, prompt_ids,
                                               [bool(prompt_ids) and random.random() < 0.5 for _ in rows],
                                               device, torch.float32)
            with torch.autocast(device_type="cuda", dtype=torch.float16, enabled=use_amp):
                loss = model(input_features=feats, decoder_input_ids=inputs, labels=labels).loss
            opt.zero_grad()
            scaler.scale(loss).backward()
            scaler.unscale_(opt)
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            scaler.step(opt)
            scaler.update()
            sched.step()
            total += loss.item()
        n = (len(train) + args.batch - 1) // args.batch
        print(f"epoch {epoch + 1}/{args.epochs}: loss {total / max(n, 1):.3f}, "
              f"test WER {evaluate(model, processor, test, device, torch.float32):.1%}")

    model = model.merge_and_unload()
    after = evaluate(model, processor, test, device, torch.float32)
    print(f"WER before {before:.1%} -> after {after:.1%}")
    if after > before:
        print("Worse than the base model — try fewer --epochs or a lower --lr, or record more data.")

    hf_dir = args.out + "-hf"
    model.save_pretrained(hf_dir)
    processor.save_pretrained(hf_dir)
    processor.feature_extractor.save_pretrained(hf_dir)   # preprocessor_config.json (transformers 5 bundles it otherwise)
    processor.tokenizer.save_pretrained(hf_dir)           # tokenizer.json
    from ctranslate2.converters import TransformersConverter
    shutil.rmtree(args.out, ignore_errors=True)
    TransformersConverter(hf_dir, copy_files=["tokenizer.json", "preprocessor_config.json"]).convert(
        args.out, quantization="float16")
    shutil.make_archive(args.out, "zip", args.out)
    print(f"\nDone: {args.out}.zip — unzip it into jarvis/models/{os.path.basename(args.out)}")


if __name__ == "__main__":
    main()
