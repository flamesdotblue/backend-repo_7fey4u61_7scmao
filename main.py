import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from bson import ObjectId

from database import db, create_document, get_documents
from schemas import Product as ProductSchema, TryOnSession as TryOnSessionSchema

import requests

app = FastAPI(title="VisionFit API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Helpers
class IdModel(BaseModel):
    id: str

def oid(id_str: str) -> ObjectId:
    try:
        return ObjectId(id_str)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid id")

@app.get("/")
def root():
    return {"name": "VisionFit Backend", "status": "ok"}

@app.get("/test")
def test_database():
    response = {"backend": "✅ Running", "database": "❌ Not Available"}
    try:
        if db is not None:
            response["database"] = "✅ Connected"
            response["collections"] = db.list_collection_names()
        else:
            response["database"] = "❌ Not Configured"
    except Exception as e:
        response["database"] = f"⚠️ {str(e)[:80]}"
    response["fal_live"] = os.getenv("FAL_LIVE", "false")
    return response

# Products
@app.post("/v1/products")
def create_product(product: ProductSchema):
    product_id = create_document("product", product)
    return {"id": product_id}

@app.get("/v1/products")
def list_products():
    items = get_documents("product")
    # cast ObjectId to str
    for it in items:
        it["id"] = str(it.pop("_id"))
    return {"items": items}

@app.get("/v1/products/{product_id}")
def get_product(product_id: str):
    doc = db["product"].find_one({"_id": oid(product_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Product not found")
    doc["id"] = str(doc.pop("_id"))
    return doc

# Try-on sessions
class CreateSessionBody(BaseModel):
    product_id: str
    mode: Optional[str] = "face"
    source_image_url: Optional[str] = None

@app.post("/v1/tryon/sessions")
def create_tryon_session(body: CreateSessionBody):
    # Validate product exists
    prod = db["product"].find_one({"_id": oid(body.product_id)})
    if not prod:
        raise HTTPException(status_code=404, detail="Product not found")

    session = TryOnSessionSchema(
        product_id=body.product_id,
        mode=body.mode or "face",
        source_image_url=body.source_image_url,
        status="processing",
    )
    session_id = create_document("tryonsession", session)

    # Sandbox mode by default to avoid external costs
    fal_live = os.getenv("FAL_LIVE", "false").lower() == "true"
    result_url: Optional[str] = None
    message: Optional[str] = None

    if fal_live:
        # Minimal example of calling FAL.ai (replace endpoint/model with your chosen one)
        try:
            fal_key = os.getenv("FAL_KEY")
            if not fal_key:
                raise Exception("FAL_KEY not configured")
            # Example call (this endpoint is illustrative)
            # Replace with actual FAL model and payload when going live
            resp = requests.post(
                "https://fal.run/fal-ai/your-model/execute",
                headers={"Authorization": f"Key {fal_key}", "Content-Type": "application/json"},
                json={
                    "image_url": body.source_image_url,
                    "mode": body.mode,
                    "product": {
                        "title": prod.get("title"),
                        "model_url": prod.get("model_url"),
                        "type": prod.get("type"),
                    },
                },
                timeout=30,
            )
            if resp.status_code != 200:
                raise Exception(f"FAL error {resp.status_code}: {resp.text[:120]}")
            data = resp.json()
            # Suppose the API returns { result_url: "..." }
            result_url = data.get("result_url")
            message = "processed via FAL"
        except Exception as e:
            db["tryonsession"].update_one({"_id": oid(session_id)}, {"$set": {"status": "failed", "message": str(e)[:200]}})
            raise HTTPException(status_code=502, detail=f"Upstream error: {str(e)[:160]}")
    else:
        # Demo result (no credits). Using a generic sample output.
        result_url = "https://images.unsplash.com/photo-1518544801976-3e188ae8e8d1?w=1600&auto=format&fit=crop&q=80"
        message = "sandbox demo (no credits used)"

    db["tryonsession"].update_one(
        {"_id": oid(session_id)},
        {"$set": {"status": "completed", "result_url": result_url, "message": message}},
    )

    return {"id": session_id, "status": "completed", "result_url": result_url, "message": message}

@app.get("/v1/tryon/sessions")
def list_sessions():
    items = get_documents("tryonsession")
    for it in items:
        it["id"] = str(it.pop("_id"))
    return {"items": items}

@app.get("/v1/tryon/sessions/{session_id}")
def get_session(session_id: str):
    doc = db["tryonsession"].find_one({"_id": oid(session_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Session not found")
    doc["id"] = str(doc.pop("_id"))
    return doc

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
