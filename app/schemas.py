from pydantic import BaseModel, ConfigDict, Field


# EmailStr rechaza dominios reservados como .local. Ese dominio se usa
# intencionalmente para las cuentas seed de desarrollo de SelfIQ.
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class RegisterRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    email: str = Field(min_length=3, max_length=320, pattern=EMAIL_PATTERN)
    password: str = Field(min_length=8, max_length=128)
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    phone: str | None = Field(default=None, max_length=30)
    registration_token: str = Field(min_length=1, max_length=255)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320, pattern=EMAIL_PATTERN)
    password: str


class UserResponse(BaseModel):
    id: str
    email: str = Field(min_length=3, max_length=320, pattern=EMAIL_PATTERN)
    first_name: str
    last_name: str
    is_superuser: bool
    account_status: str
    role: str
    organization_id: str
    branch_id: str | None = None


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class RegisterResponse(BaseModel):
    message: str
    user_id: str
    account_status: str


class BranchCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    registration_token: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=2, max_length=150)
    address: str = Field(min_length=5, max_length=255)
    timezone: str = Field(min_length=1, max_length=64)
    category: str = Field(min_length=1, max_length=100)


class BranchResponse(BaseModel):
    id: str
    name: str
    code: str
    address: str
    timezone: str
    category: str
    business_id: str


class BranchCreatedResponse(BranchResponse):
    # Returned only on creation; the original token isn't stored/recoverable.
    branch_token: str


class BusinessTokenResponse(BaseModel):
    business_id: str
    business_token: str


class BranchTokenResponse(BaseModel):
    branch_id: str
    branch_token: str