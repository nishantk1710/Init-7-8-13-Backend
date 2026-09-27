-- Casting helpers for the raw extract layer -- the SQL Server (Azure SQL) twin
-- of 00_functions.postgres.sql. Same contract: return NULL rather than raise,
-- because one malformed cell must not take a whole view down.
--
-- Each CREATE FUNCTION must be alone in its batch on SQL Server, so views.py
-- executes this file split on the marker lines below.
--
-- The views call these as dbo.sap_key(...) etc.; views.py adds the schema
-- prefix when it renders the view scripts for SQL Server.

-- SAP key -> canonical form: leading zeros stripped, blank becomes NULL.
create or alter function dbo.sap_key(@value nvarchar(4000)) returns nvarchar(4000)
as
begin
    return nullif(substring(trim(@value), patindex('%[^0]%', trim(@value) + '.'), 4000), '')
end

-- @@batch

-- ISO text -> date. Anything that is not YYYY-MM-DD is NULL (the normalise
-- views have already turned DD.MM.YYYY and YYYYMMDD into ISO).
create or alter function dbo.sap_date(@value nvarchar(4000)) returns date
as
begin
    return case
        when trim(@value) like '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
            then try_convert(date, trim(@value), 23)
    end
end

-- @@batch

-- Text -> number, or NULL when it is not one.
create or alter function dbo.sap_num(@value nvarchar(4000)) returns decimal(38, 6)
as
begin
    return try_cast(trim(@value) as decimal(38, 6))
end
