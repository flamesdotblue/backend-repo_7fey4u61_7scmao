import os
import secrets
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from typing import Optional, Literal
from bson import ObjectId

from database import db, create_document, get_documents
from schemas import Product as ProductSchema, TryOnSession as TryOnSessionSchema, Organization as OrganizationSchema, ApiKey as ApiKeySchema, User as UserSchema

import requests
from passlib.context import CryptContext
import jwt

app = FastAPI(title="VisionFit API", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Security helpers
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-me")
JWT_ALG = "HS256"
JWT_EXP_MIN = int(os.getenv("JWT_EXP_MIN", "1440"))  # 24h default

# Helpers
class IdModel(BaseModel):
    id: str

def oid(id_str: str) -> ObjectId:
    try:
        return ObjectId(id_str)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid id")

# Auth models
class SignupBody(BaseModel):
    name: str
    email: EmailStr
    password: str
    organization_name: str

class LoginBody(BaseModel):
    email: EmailStr
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"

class MeResponse(BaseModel):
    id: str
    name: str
    email: EmailStr
    role: Literal['admin','member']
    organization_id: str

# Token utils

def create_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=JWT_EXP_MIN))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALG)


def get_current_user(authorization: str = Header(None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
        uid = payload.get("sub")
        if not uid:
            raise Exception("Invalid token")
        doc = db["user"].find_one({"_id": oid(uid)})
        if not doc:
            raise Exception("User not found")
        doc["id"] = str(doc.pop("_id"))
        return doc
    except Exception as e:
        raise HTTPException(status_code=401, detail="Invalid token")


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

# ============ AUTH & ORGS ============
@app.post("/v1/auth/signup", response_model=TokenResponse)
def signup(body: SignupBody):
    # ensure email unique
    if db["user"].find_one({"email": body.email}):
        raise HTTPException(status_code=409, detail="Email already registered")
    # create org
    slug = body.organization_name.lower().strip().replace(" ", "-")
    org = OrganizationSchema(name=body.organization_name, slug=slug, plan='free')
    org_id = create_document("organization", org)
    # create user
    password_hash = pwd_context.hash(body.password)
    user = UserSchema(name=body.name, email=body.email, password_hash=password_hash, organization_id=org_id, role='admin')
    user_id = create_document("user", user)
    # bootstrap an API key for convenience
    raw_key = "vf_" + secrets.token_urlsafe(24)
    api_key = ApiKeySchema(organization_id=org_id, label="Default Key", key=raw_key)
    _ = create_document("apikey", api_key)
    # token
    token = create_token({"sub": user_id, "org": org_id, "role": "admin"})
    return TokenResponse(access_token=token)


@app.post("/v1/auth/login", response_model=TokenResponse)
def login(body: LoginBody):
    doc = db["user"].find_one({"email": body.email})
    if not doc:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not pwd_context.verify(body.password, doc.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    user_id = str(doc["_id"]) 
    org_id = doc.get("organization_id")
    role = doc.get("role", "member")
    token = create_token({"sub": user_id, "org": org_id, "role": role})
    return TokenResponse(access_token=token)


@app.get("/v1/me", response_model=MeResponse)
def me(current_user: dict = Depends(get_current_user)):
    return MeResponse(
        id=current_user["id"],
        name=current_user.get("name"),
        email=current_user.get("email"),
        role=current_user.get("role"),
        organization_id=current_user.get("organization_id"),
    )


# Org + API keys
class ApiKeyCreateBody(BaseModel):
    label: str
    scopes: Optional[list[str]] = None

@app.get("/v1/org")
def get_org(current_user: dict = Depends(get_current_user)):
    org = db["organization"].find_one({"_id": oid(current_user["organization_id"])})
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    org["id"] = str(org.pop("_id"))
    return org

@app.get("/v1/org/apikeys")
def list_api_keys(current_user: dict = Depends(get_current_user)):
    items = get_documents("apikey", {"organization_id": current_user["organization_id"]})
    for it in items:
        it["id"] = str(it.pop("_id"))
    return {"items": items}

@app.post("/v1/org/apikeys")
def create_api_key(body: ApiKeyCreateBody, current_user: dict = Depends(get_current_user)):
    raw_key = "vf_" + secrets.token_urlsafe(24)
    api_key = ApiKeySchema(
        organization_id=current_user["organization_id"],
        label=body.label,
        key=raw_key,
        scopes=body.scopes or ['tryon:read','tryon:write'],
        active=True,
    )
    key_id = create_document("apikey", api_key)
    return {"id": key_id, "key": raw_key}

@app.post("/v1/org/apikeys/{key_id}/revoke")
def revoke_api_key(key_id: str, current_user: dict = Depends(get_current_user)):
    res = db["apikey"].update_one({"_id": oid(key_id), "organization_id": current_user["organization_id"]}, {"$set": {"active": False, "updated_at": datetime.now(timezone.utc)}})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="API key not found")
    return {"status": "revoked"}


# ============ PRODUCTS ============
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


# ============ TRY-ON SESSIONS ============
class CreateSessionBody(BaseModel):
    product_id: str
    mode: Optional[str] = "face"
    source_image_url: Optional[str] = None


def validate_api_key(x_api_key: Optional[str]) -> Optional[dict]:
    if not x_api_key:
        return None
    key_doc = db["apikey"].find_one({"key": x_api_key, "active": True})
    if not key_doc:
        raise HTTPException(status_code=401, detail="Invalid API key")
    key_doc["id"] = str(key_doc.pop("_id"))
    return key_doc

@app.post("/v1/tryon/sessions")
def create_tryon_session(body: CreateSessionBody, x_api_key: Optional[str] = Header(None)):
    # Validate API key if provided or if required by env
    require_key = os.getenv("TRYON_REQUIRE_API_KEY", "false").lower() == "true"
    key_info = validate_api_key(x_api_key) if (x_api_key or require_key) else None

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
                    "metadata": {"apikey_id": key_info.get("id") if key_info else None},
                },
                timeout=30,
            )
            if resp.status_code != 200:
                raise Exception(f"FAL error {resp.status_code}: {resp.text[:120]}")
            data = resp.json()
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
