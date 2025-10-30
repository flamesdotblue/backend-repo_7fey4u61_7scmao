from pydantic import BaseModel, Field, HttpUrl, EmailStr
from typing import Optional, Literal

# Organization represents a client account
class Organization(BaseModel):
    name: str = Field(..., description="Organization display name")
    slug: str = Field(..., description="Unique slug identifier")
    plan: Literal['free', 'pro', 'enterprise'] = Field('free')

# ApiKey stored per organization
class ApiKey(BaseModel):
    organization_id: str = Field(..., description="Reference to organization")
    label: str = Field(..., description="Key label")
    key: str = Field(..., description="Hashed or plain for demo")
    scopes: list[str] = Field(default_factory=lambda: ['tryon:read', 'tryon:write'])
    active: bool = Field(True)

# User account
class User(BaseModel):
    name: str
    email: EmailStr
    password_hash: str
    organization_id: str
    role: Literal['admin','member'] = Field('admin')

# Product catalog entry to try on
class Product(BaseModel):
    title: str
    sku: Optional[str] = None
    type: Literal['eyewear','headset','hat','jewelry'] = Field('eyewear')
    model_url: Optional[HttpUrl] = None
    thumbnail_url: Optional[HttpUrl] = None

# Try-on session record
class TryOnSession(BaseModel):
    product_id: str
    mode: Literal['face','head'] = Field('face')
    source_image_url: Optional[HttpUrl] = None
    status: Literal['queued','processing','completed','failed'] = Field('queued')
    result_url: Optional[HttpUrl] = None
    message: Optional[str] = None
