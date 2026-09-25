-- LFA1 -- vendor master, business fields ONLY.
--
-- POPIA. raw_lfa1 has 179 columns including personal contact details, date of
-- birth and BEE ownership data. This view promotes four business-entity fields
-- and nothing else, so no API serving it can leak the rest by accident. Widen
-- it deliberately or not at all -- never with SELECT *.
--
-- Coverage is thin: 106 vendors in total, and only 4 of the 61 vendors on
-- repair POs resolve to a name here. Vendor analytics must therefore fall back
-- to the vendor code and say so, rather than dropping the unnamed vendors.
create or replace view v_lfa1 as
select
    sap_key(vendor)        as lifnr,
    vendor                 as lifnr_raw,
    nullif(name_1, '')     as name1,
    nullif(country, '')    as land1,
    nullif(city, '')       as ort01
from raw_lfa1;
