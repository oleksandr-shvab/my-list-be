import uuid
from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator


class UserRegister(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    is_active: bool
    created_at: datetime


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class PasswordConfirmationMixin(BaseModel):
    new_password: str = Field(min_length=8, max_length=128)
    confirm_password: str = Field(min_length=8, max_length=128)

    @model_validator(mode="after")
    def passwords_match(self) -> Self:
        if self.new_password != self.confirm_password:
            raise ValueError("Passwords do not match")
        return self


class ResetPasswordRequest(PasswordConfirmationMixin):
    token: str


class UpdatePasswordRequest(PasswordConfirmationMixin):
    old_password: str

    @model_validator(mode="after")
    def new_password_differs_from_old(self) -> Self:
        if self.new_password == self.old_password:
            raise ValueError("New password must be different from the old password")
        return self
