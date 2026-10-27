"""Spike: Chinese-CLIP ONNX dual-tower smoke test (no torch).

Feeds synthetic colored-shape images + Chinese text queries through the
quantized ONNX graph and asserts cosine-similarity ranking is sane.
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image, ImageDraw
from transformers import AutoImageProcessor, AutoTokenizer

MODEL_DIR = Path("F:/Code/photo-album/.data/models/chinese_clip_onnx")
vs = ort.InferenceSession(str(MODEL_DIR / "onnx" / "vision.onnx"),
                          providers=["CPUExecutionProvider"],
                          intra_op_num_threads=4)
ts = ort.InferenceSession(str(MODEL_DIR / "onnx" / "text.onnx"),
                          providers=["CPUExecutionProvider"],
                          intra_op_num_threads=4)
print("vision:", [(i.name, i.shape) for i in vs.get_inputs()],
      "->", [o.name for o in vs.get_outputs()])
print("text:", [(i.name, i.shape) for i in ts.get_inputs()],
      "->", [o.name for o in ts.get_outputs()])

tok = AutoTokenizer.from_pretrained(MODEL_DIR)
ip = AutoImageProcessor.from_pretrained(MODEL_DIR)


def img_embed(img: Image.Image) -> np.ndarray:
    px = ip(images=img, return_tensors="np")["pixel_values"]
    out = vs.run(None, {"pixel_values": px.astype(np.float32)})[0]
    v = out / np.linalg.norm(out, axis=-1, keepdims=True)
    return v[0]


def text_embed(texts: list[str]) -> np.ndarray:
    enc = tok(text=texts, padding="max_length", truncation=True,
              max_length=52, return_tensors="np")
    out = ts.run(None, {
        "input_ids": enc["input_ids"].astype(np.int64),
        "attention_mask": enc["attention_mask"].astype(np.int64)})[0]
    v = out / np.linalg.norm(out, axis=-1, keepdims=True)
    return v


def scene(bg, shape, color):
    img = Image.new("RGB", (224, 224), bg)
    d = ImageDraw.Draw(img)
    if shape == "circle":
        d.ellipse([52, 52, 172, 172], fill=color)
    else:
        d.rectangle([52, 52, 172, 172], fill=color)
    return img


images = {
    "red_square": scene("white", "rect", "red"),
    "blue_circle": scene("black", "circle", "blue"),
    "green_square": scene("white", "rect", "green"),
}
queries = ["红色的正方形", "蓝色的圆形", "绿色的方块"]

t0 = time.time()
IE = {k: img_embed(v) for k, v in images.items()}
TE = text_embed(queries)
print(f"encode {len(IE)} img + {len(queries)} text in {time.time()-t0:.2f}s")

S = TE @ np.stack(list(IE.values())).T          # [q, img]
names = list(IE.keys())
for qi, q in enumerate(queries):
    order = np.argsort(-S[qi])
    print(f"{q!r:12} -> " + ", ".join(f"{names[j]}={S[qi, j]:.3f}" for j in order))

expect = {"红色的正方形": "red_square", "蓝色的圆形": "blue_circle", "绿色的方块": "green_square"}
bad = [q for q, want in expect.items()
       if names[int(np.argmax(S[queries.index(q)]))] != want]
assert not bad, f"ranking wrong for {bad}"

# image-to-image consistency: same image twice -> similarity ~1.0
assert float(IE["red_square"] @ IE["red_square"]) > 0.999
print("\nSPIKE PASSED")
