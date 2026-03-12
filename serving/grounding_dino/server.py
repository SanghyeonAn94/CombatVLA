import io
import torch
import numpy as np
from PIL import Image
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import JSONResponse
import groundingdino.datasets.transforms as T
from groundingdino.util.inference import load_model

app = FastAPI(title="GroundingDINO API")

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
MODEL = None


def load_and_transform_image(image_bytes: bytes):
    image_pil = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = image_pil.size
    transform = T.Compose([
        T.RandomResize([800], max_size=1333),
        T.ToTensor(),
        T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
    ])
    image_tensor, _ = transform(image_pil, None)
    return image_tensor, w, h


def predict(image_tensor, caption, box_threshold):
    caption = caption.lower().strip()
    if not caption.endswith("."):
        caption += " ."

    with torch.no_grad():
        outputs = MODEL(image_tensor[None].to(DEVICE), captions=[caption])

    logits = outputs["pred_logits"].cpu().sigmoid()[0]
    boxes = outputs["pred_boxes"].cpu()[0]

    mask = logits.max(dim=1)[0] > box_threshold
    filtered_logits = logits[mask]
    filtered_boxes = boxes[mask]

    # 라벨 매칭 (object_utils.py 로직 기반)
    input_text = caption.split()
    phrases = []
    confidences = []
    for logit in filtered_logits:
        prob = logit[logit > 0][1:-1]
        max_prob, cum_prob, pre_i, label = 0, 0, 0, ""
        for i, (c, p) in enumerate(zip(input_text, prob)):
            if c == ".":
                if cum_prob > max_prob:
                    max_prob = cum_prob
                    label = " ".join(input_text[pre_i:i])
                cum_prob = 0
                pre_i = i + 1
            else:
                cum_prob += p
        phrases.append(label)
        confidences.append(float(logit.max()))

    return filtered_boxes, confidences, phrases


@app.on_event("startup")
def startup():
    global MODEL
    MODEL = load_model(
        "/models/GroundingDINO_SwinB_cfg.py",
        "/models/groundingdino_swinb_cogcoor.pth",
    )
    MODEL = MODEL.to(DEVICE)
    MODEL.eval()


@app.get("/health")
def health():
    return {"status": "ok", "device": DEVICE, "model_loaded": MODEL is not None}


@app.post("/detect")
async def detect(
    image: UploadFile = File(...),
    prompt: str = Form(...),
    box_threshold: float = Form(0.3),
):
    image_bytes = await image.read()
    image_tensor, w, h = load_and_transform_image(image_bytes)
    boxes, confidences, phrases = predict(image_tensor, prompt, box_threshold)

    detections = []
    for box, conf, label in zip(boxes, confidences, phrases):
        # cxcywh (normalized) → xyxy (pixel)
        cx, cy, bw, bh = box.tolist()
        x1 = int((cx - bw / 2) * w)
        y1 = int((cy - bh / 2) * h)
        x2 = int((cx + bw / 2) * w)
        y2 = int((cy + bh / 2) * h)
        detections.append({
            "label": label,
            "bbox": [x1, y1, x2, y2],
            "confidence": round(conf, 4),
        })

    return JSONResponse({"detections": detections})
