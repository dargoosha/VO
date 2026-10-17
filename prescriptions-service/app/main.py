import os
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field, ConfigDict, field_validator

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import (
    create_engine, text, select, func, String,
    Integer, Date, Numeric, BigInteger
)
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, mapped_column, Session
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import URL


class Base(DeclarativeBase):
    pass


class Prescription(Base):
    __tablename__ = "prescriptions"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True
    )
    patient_code: Mapped[str] = mapped_column(String(50))
    drug: Mapped[str] = mapped_column(String(200))
    dose: Mapped[Decimal] = mapped_column(Numeric(10, 3))
    unit: Mapped[str] = mapped_column(String(30))
    frequency_per_day: Mapped[int] = mapped_column(Integer)
    start_date: Mapped[date] = mapped_column(Date)
    duration_days: Mapped[int] = mapped_column(Integer)


class PrescriptionInput(BaseModel):
    model_config = ConfigDict(
        str_strip_whitespace=True,
        extra="forbid"
    )

    patient_code: str = Field(min_length=1, max_length=50)
    drug: str = Field(min_length=1, max_length=200)
    dose: Decimal = Field(gt=0, max_digits=10, decimal_places=3)
    unit: str = Field(min_length=1, max_length=30)
    frequency_per_day: int = Field(ge=1, le=6, strict=True)
    start_date: date
    duration_days: int = Field(ge=1, le=180, strict=True)
    @field_validator("patient_code", "drug", "unit")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError(
                "Control characters are not allowed"
            )
        return value


class PrescriptionOutput(PrescriptionInput):
    id: int

    model_config = ConfigDict(from_attributes=True)


db_url = URL.create(
    "postgresql+psycopg",
    username=os.environ["POSTGRES_USER"],
    password=os.environ["POSTGRES_PASSWORD"],
    host=os.getenv("DB_HOST", "db"),
    port=5432,
    database=os.environ["POSTGRES_DB"],
)

engine = create_engine(
    db_url,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=0,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    engine.dispose()


app = FastAPI(
    title="Prescriptions CRUD API",
    lifespan=lifespan
)


@app.exception_handler(RequestValidationError)
async def validation_error(
    request: Request,
    exc: RequestValidationError
):
    errors = exc.errors()

    for error in errors:
        error.pop("ctx", None)

    return JSONResponse(
        status_code=400,
        content={
            "detail": jsonable_encoder(errors)
        }
    )
@app.get("/noop")
async def noop():
    return None

@app.get("/healthz")
def healthz():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable"
        )


@app.post(
    "/prescriptions",
    response_model=PrescriptionOutput,
    status_code=201
)
def create_prescription(data: PrescriptionInput):
    with Session(engine) as session:
        item = Prescription(**data.model_dump())
        session.add(item)
        try:
            session.commit()
            session.refresh(item)
        except IntegrityError:
            session.rollback()
            raise HTTPException(400, "Invalid prescription data")
        return item


@app.get("/prescriptions")
def list_prescriptions(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    drug: str | None = None
):
    with Session(engine) as session:
        condition = (
            func.lower(Prescription.drug) == drug.lower()
            if drug is not None else None
        )

        query = select(Prescription)
        count_query = select(func.count()).select_from(Prescription)

        if condition is not None:
            query = query.where(condition)
            count_query = count_query.where(condition)

        total = session.scalar(count_query)
        items = session.scalars(
            query.order_by(Prescription.id)
                 .limit(limit)
                 .offset(offset)
        ).all()

        return {
            "items": [
                PrescriptionOutput.model_validate(item)
                for item in items
            ],
            "total": total
        }


@app.get(
    "/prescriptions/{id}",
    response_model=PrescriptionOutput
)
def get_prescription(id: int):
    with Session(engine) as session:
        item = session.get(Prescription, id)
        if item is None:
            raise HTTPException(404, "Prescription not found")
        return item


@app.put(
    "/prescriptions/{id}",
    response_model=PrescriptionOutput
)
def update_prescription(id: int, data: PrescriptionInput):
    with Session(engine) as session:
        item = session.get(Prescription, id)
        if item is None:
            raise HTTPException(404, "Prescription not found")

        for key, value in data.model_dump().items():
            setattr(item, key, value)

        try:
            session.commit()
            session.refresh(item)
        except IntegrityError:
            session.rollback()
            raise HTTPException(400, "Invalid prescription data")
        return item


@app.delete("/prescriptions/{id}", status_code=204)
def delete_prescription(id: int):
    with Session(engine) as session:
        item = session.get(Prescription, id)
        if item is None:
            raise HTTPException(404, "Prescription not found")

        session.delete(item)
        session.commit()