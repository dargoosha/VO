CREATE TABLE IF NOT EXISTS prescriptions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    patient_code VARCHAR(50) NOT NULL,
    drug VARCHAR(200) NOT NULL,
    dose NUMERIC(10,3) NOT NULL CHECK (dose > 0),
    unit VARCHAR(30) NOT NULL,
    frequency_per_day INTEGER NOT NULL
        CHECK (frequency_per_day BETWEEN 1 AND 6),
    start_date DATE NOT NULL,
    duration_days INTEGER NOT NULL
        CHECK (duration_days BETWEEN 1 AND 180)
);

CREATE INDEX IF NOT EXISTS idx_prescriptions_drug
ON prescriptions (LOWER(drug));