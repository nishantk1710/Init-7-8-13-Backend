-- Casting helpers for the raw extract layer.
--
-- Every raw_* column is text (app/seed/loader.py types them that way on
-- purpose -- a numeric coercion at load time turns 000000008000000000 into
-- 8e+15 irreversibly). The views above these functions are where text becomes
-- a date, a number and a canonical key.
--
-- They are written to return NULL rather than raise. One malformed cell in
-- 3.37M rows must not take a whole view down: a NULL is a visible gap the API
-- can report, an exception is an outage.

-- SAP key -> canonical form: leading zeros stripped, blank becomes NULL.
--
-- This is ruling 5.1 of the task plan, enforced in the one place every read
-- passes through. The July extract carries no leading zeros, so today this
-- changes no rows -- which is exactly why it has to go in now. At CPI cutover
-- the same material arrives as '000000008000000000' and a LIKE '80%' test
-- silently returns nothing.
--
-- Used for MATNR, LIFNR and EBELN alike: SAP zero-pads all of them, and the
-- rule "never compare two raw keys" applies to every one.
create or replace function sap_key(value text) returns text
    language sql
    immutable
    returns null on null input
as $$
    select nullif(ltrim(btrim(value), '0'), '')
$$;

-- Text -> date, or NULL when the cell is not an ISO date.
--
-- Measured on 11-Sep: every date cell in raw_eket, raw_ekko, raw_ekbe and
-- raw_mseg is already 'YYYY-MM-DD'. The regex guard is for the cell that is
-- not, in a later extract.
create or replace function sap_date(value text) returns date
    language sql
    immutable
    returns null on null input
as $$
    select case when btrim(value) ~ '^\d{4}-\d{2}-\d{2}$' then btrim(value)::date end
$$;

-- Text -> numeric, or NULL when the cell is not a plain number.
--
-- Deliberately strict: no thousands separators, no currency symbols. If a
-- future extract carries '1,234.00' this returns NULL and the gap shows up in
-- the API, rather than 1234 being guessed or 1 being parsed.
create or replace function sap_num(value text) returns numeric
    language sql
    immutable
    returns null on null input
as $$
    select case
        when btrim(value) ~ '^-?[0-9]+(\.[0-9]+)?$' then btrim(value)::numeric
    end
$$;
