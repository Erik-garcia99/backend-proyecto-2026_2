-- Migration: customer organization code for client registration
-- Adds a unique, nullable customer_code to organization.
-- Staff (owner/admin) can set/regenerate it via API.

ALTER TABLE organization
    ADD COLUMN IF NOT EXISTS customer_code text UNIQUE;

CREATE INDEX IF NOT EXISTS idx_organization_customer_code
    ON organization (customer_code)
    WHERE customer_code IS NOT NULL;
