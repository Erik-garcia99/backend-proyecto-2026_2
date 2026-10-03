-- ============================================================
-- SelfIQ - Sprint 1
-- PostgreSQL schema
-- Prepared for future multi-branch support
-- ============================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'movement_type_enum') THEN
        CREATE TYPE movement_type_enum AS ENUM ('SALE','ENTRY','EXIT','ADJUSTMENT');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'sale_status_enum') THEN
        CREATE TYPE sale_status_enum AS ENUM ('PENDING','COMPLETED','CANCELLED');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'occupancy_status_enum') THEN
        CREATE TYPE occupancy_status_enum AS ENUM ('AVAILABLE','NEAR_CAPACITY','FULL');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'iot_node_status_enum') THEN
        CREATE TYPE iot_node_status_enum AS ENUM ('ONLINE','OFFLINE','UNKNOWN');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'iot_node_type_enum') THEN
        CREATE TYPE iot_node_type_enum AS ENUM ('OCCUPANCY','SHELF_CAMERA','LED_CONTROLLER','ENVIRONMENT','GATEWAY');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'user_account_status_enum') THEN
        CREATE TYPE user_account_status_enum AS ENUM ('PENDING_APPROVAL','ACTIVE','REJECTED','SUSPENDED');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'user_branch_status_enum') THEN
        CREATE TYPE user_branch_status_enum AS ENUM ('PENDING','ACTIVE','REJECTED','SUSPENDED');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'registration_token_type_enum') THEN
        CREATE TYPE registration_token_type_enum AS ENUM ('USER_REGISTRATION','BRANCH_CREATION');
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS business (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(150) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS branch (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id UUID NOT NULL REFERENCES business(id),
    name VARCHAR(150) NOT NULL,
    code VARCHAR(50) NOT NULL,
    address VARCHAR(255),
    timezone VARCHAR(64) NOT NULL DEFAULT 'America/Mexico_City',
    business_category VARCHAR(100) NOT NULL DEFAULT 'General',
    capacity INTEGER CHECK (capacity IS NULL OR capacity >= 0),
    is_main BOOLEAN NOT NULL DEFAULT FALSE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_branch_business_code UNIQUE (business_id, code)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_business_main_branch ON branch (business_id) WHERE is_main = TRUE;

CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    first_name VARCHAR(100) NOT NULL,
    last_name VARCHAR(100) NOT NULL,
    phone VARCHAR(30),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    is_superuser BOOLEAN NOT NULL DEFAULT FALSE,
    account_status user_account_status_enum NOT NULL DEFAULT 'PENDING_APPROVAL',
    last_login_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS role (
    id SMALLSERIAL PRIMARY KEY,
    name VARCHAR(50) NOT NULL UNIQUE,
    description VARCHAR(255)
);

INSERT INTO role (name, description)
VALUES
    ('SUDO', 'Global system superuser with unrestricted access'),
    ('OWNER', 'Business owner'),
    ('MANAGER', 'Branch manager'),
    ('EMPLOYEE', 'Store employee'),
    ('CASHIER', 'POS cashier'),
    ('CUSTOMER', 'Store customer')
ON CONFLICT (name) DO NOTHING;

CREATE TABLE IF NOT EXISTS user_branch (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    branch_id UUID NOT NULL REFERENCES branch(id),
    role_id SMALLINT REFERENCES role(id),
    membership_status user_branch_status_enum NOT NULL DEFAULT 'PENDING',
    approved_by UUID REFERENCES users(id),
    approved_at TIMESTAMPTZ,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_user_branch UNIQUE (user_id, branch_id)
);

CREATE TABLE IF NOT EXISTS branch_registration_token (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id UUID NOT NULL REFERENCES branch(id),
    token_hash VARCHAR(255) NOT NULL UNIQUE,
    token_type registration_token_type_enum NOT NULL,
    created_by UUID NOT NULL REFERENCES users(id),
    expires_at TIMESTAMPTZ,
    max_uses INTEGER CHECK (max_uses IS NULL OR max_uses > 0),
    uses INTEGER NOT NULL DEFAULT 0 CHECK (uses >= 0),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_registration_token_uses CHECK (max_uses IS NULL OR uses <= max_uses)
);

CREATE TABLE IF NOT EXISTS employee (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL UNIQUE REFERENCES users(id),
    employee_number VARCHAR(50),
    hire_date DATE,
    job_title VARCHAR(100),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS customer (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL UNIQUE REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS supplier (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id UUID NOT NULL REFERENCES business(id),
    name VARCHAR(150) NOT NULL,
    email VARCHAR(255),
    phone VARCHAR(30),
    contact_name VARCHAR(150),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS product (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id UUID NOT NULL REFERENCES business(id),
    sku VARCHAR(100) NOT NULL,
    barcode VARCHAR(100),
    name VARCHAR(200) NOT NULL,
    description TEXT,
    category VARCHAR(100),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_product_business_sku UNIQUE (business_id, sku),
    CONSTRAINT uq_product_business_barcode UNIQUE (business_id, barcode)
);

CREATE TABLE IF NOT EXISTS supplier_product (
    supplier_id UUID NOT NULL REFERENCES supplier(id),
    product_id UUID NOT NULL REFERENCES product(id),
    supplier_sku VARCHAR(100),
    cost NUMERIC(12,2) CHECK (cost IS NULL OR cost >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (supplier_id, product_id)
);

CREATE TABLE IF NOT EXISTS branch_product (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id UUID NOT NULL REFERENCES branch(id),
    product_id UUID NOT NULL REFERENCES product(id),
    price NUMERIC(12,2) NOT NULL CHECK (price >= 0),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_branch_product UNIQUE (branch_id, product_id)
);

CREATE TABLE IF NOT EXISTS inventory (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_product_id UUID NOT NULL UNIQUE REFERENCES branch_product(id),
    quantity INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS inventory_movement (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id UUID NOT NULL REFERENCES branch(id),
    product_id UUID NOT NULL REFERENCES product(id),
    user_id UUID REFERENCES users(id),
    movement_type movement_type_enum NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity <> 0),
    reference_type VARCHAR(50),
    reference_id UUID,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sale (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id UUID NOT NULL REFERENCES branch(id),
    cashier_user_id UUID NOT NULL REFERENCES users(id),
    subtotal NUMERIC(12,2) NOT NULL CHECK (subtotal >= 0),
    total NUMERIC(12,2) NOT NULL CHECK (total >= 0),
    status sale_status_enum NOT NULL DEFAULT 'PENDING',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sale_item (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sale_id UUID NOT NULL REFERENCES sale(id) ON DELETE CASCADE,
    product_id UUID NOT NULL REFERENCES product(id),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price NUMERIC(12,2) NOT NULL CHECK (unit_price >= 0),
    subtotal NUMERIC(12,2) NOT NULL CHECK (subtotal >= 0)
);

CREATE TABLE IF NOT EXISTS shelf (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id UUID NOT NULL REFERENCES branch(id),
    name VARCHAR(150) NOT NULL,
    code VARCHAR(50),
    position_x NUMERIC(6,5) CHECK (position_x IS NULL OR (position_x >= 0 AND position_x <= 1)),
    position_y NUMERIC(6,5) CHECK (position_y IS NULL OR (position_y >= 0 AND position_y <= 1)),
    rotation NUMERIC(6,2),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_shelf_branch_code UNIQUE (branch_id, code)
);

CREATE TABLE IF NOT EXISTS shelf_product (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    shelf_id UUID NOT NULL REFERENCES shelf(id),
    product_id UUID NOT NULL REFERENCES product(id),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_shelf_product UNIQUE (shelf_id, product_id)
);

CREATE TABLE IF NOT EXISTS branch_occupancy (
    branch_id UUID PRIMARY KEY REFERENCES branch(id),
    current_count INTEGER NOT NULL DEFAULT 0 CHECK (current_count >= 0),
    capacity INTEGER CHECK (capacity IS NULL OR capacity >= 0),
    status occupancy_status_enum NOT NULL DEFAULT 'AVAILABLE',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS iot_node (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id UUID NOT NULL REFERENCES branch(id),
    shelf_id UUID REFERENCES shelf(id),
    name VARCHAR(150) NOT NULL,
    node_type iot_node_type_enum NOT NULL,
    status iot_node_status_enum NOT NULL DEFAULT 'UNKNOWN',
    battery_level SMALLINT CHECK (battery_level IS NULL OR (battery_level >= 0 AND battery_level <= 100)),
    last_seen_at TIMESTAMPTZ,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_branch_business ON branch (business_id);
CREATE INDEX IF NOT EXISTS idx_registration_token_branch ON branch_registration_token (branch_id);
CREATE INDEX IF NOT EXISTS idx_registration_token_active_type ON branch_registration_token (branch_id, token_type, is_active);
CREATE INDEX IF NOT EXISTS idx_user_branch_status ON user_branch (branch_id, membership_status);
CREATE INDEX IF NOT EXISTS idx_users_superuser ON users (is_superuser) WHERE is_superuser = TRUE;
CREATE INDEX IF NOT EXISTS idx_user_branch_user ON user_branch (user_id);
CREATE INDEX IF NOT EXISTS idx_user_branch_branch ON user_branch (branch_id);
CREATE INDEX IF NOT EXISTS idx_supplier_business ON supplier (business_id);
CREATE INDEX IF NOT EXISTS idx_product_business ON product (business_id);
CREATE INDEX IF NOT EXISTS idx_product_name ON product (name);
CREATE INDEX IF NOT EXISTS idx_product_category ON product (category);
CREATE INDEX IF NOT EXISTS idx_branch_product_branch ON branch_product (branch_id);
CREATE INDEX IF NOT EXISTS idx_branch_product_product ON branch_product (product_id);
CREATE INDEX IF NOT EXISTS idx_inventory_movement_branch_created ON inventory_movement (branch_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_inventory_movement_product_created ON inventory_movement (product_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sale_branch_created ON sale (branch_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sale_item_sale ON sale_item (sale_id);
CREATE INDEX IF NOT EXISTS idx_shelf_branch ON shelf (branch_id);
CREATE INDEX IF NOT EXISTS idx_iot_node_branch ON iot_node (branch_id);
CREATE INDEX IF NOT EXISTS idx_iot_node_shelf ON iot_node (shelf_id);
CREATE INDEX IF NOT EXISTS idx_iot_node_status ON iot_node (branch_id, status);

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE
    tbl TEXT;
BEGIN
    FOREACH tbl IN ARRAY ARRAY[
        'business','branch','users','user_branch','employee','customer',
        'supplier','product','branch_product','sale','shelf','iot_node'
    ]
    LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS trg_%I_updated_at ON %I', tbl, tbl);
        EXECUTE format(
            'CREATE TRIGGER trg_%I_updated_at BEFORE UPDATE ON %I
             FOR EACH ROW EXECUTE FUNCTION set_updated_at()',
            tbl, tbl
        );
    END LOOP;
END
$$;

-- Authorization flow:
-- New users must register with a valid USER_REGISTRATION token.
-- The backend verifies the raw token against token_hash, validates expiry/usage,
-- creates users.account_status='PENDING_APPROVAL' and user_branch.membership_status='PENDING'.
-- Pending users should only access account/status/logout endpoints.
-- An authorized admin assigns role_id and sets membership_status='ACTIVE', approved_by and approved_at.
-- SUDO users (is_superuser=TRUE) bypass normal branch/role restrictions.
-- New branches should only be created using a valid BRANCH_CREATION token from the main branch.
-- Store only token hashes in PostgreSQL; never raw registration tokens.

-- Example SUDO seed (replace the password hash):
-- INSERT INTO users (email,password_hash,first_name,last_name,is_active,is_superuser,account_status)
-- VALUES ('sudo@selfiq.local','<ARGON2ID_HASH>','Sudo','Administrator',TRUE,TRUE,'ACTIVE');

COMMIT;
