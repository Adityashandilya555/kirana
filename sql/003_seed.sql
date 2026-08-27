-- Demo fixtures. Fixed UUIDs so scripts and tests can hardcode them.
-- Costs are set so the margin floor actually BINDS on some items -- a floor
-- that never triggers is a rule the demo cannot show working.
--
--   sku      price    cost   gross margin
--   SUGAR1     48      43     10.42%   <- below a 12% floor: no discount possible
--   OIL1L     145     128     11.72%   <- also below
--   ATTA5     285     245     14.04%
--   RICE5     620     520     16.13%
--   DAL1K     175     142     18.86%
--   TEA250    190     135     28.95%   <- room to haggle

insert into merchants (id, name, store_line) values
  ('00000000-0000-0000-0000-00000000d001',
   'Sharma Kirana Store', 'Since 1998 - Lajpat Nagar, New Delhi')
on conflict (id) do update set name = excluded.name, store_line = excluded.store_line;

insert into catalog_items (merchant_id, sku, name, unit, price_paise, cost_paise) values
  ('00000000-0000-0000-0000-00000000d001','ATTA5','Aashirvaad Whole Wheat Atta 5kg','bag',   28500, 24500),
  ('00000000-0000-0000-0000-00000000d001','RICE5','India Gate Basmati Rice 5kg',    'bag',   62000, 52000),
  ('00000000-0000-0000-0000-00000000d001','OIL1L','Fortune Sunflower Oil 1L',       'bottle',14500, 12800),
  ('00000000-0000-0000-0000-00000000d001','DAL1K','Toor Dal 1kg',                   'pack',  17500, 14200),
  ('00000000-0000-0000-0000-00000000d001','SUGAR1','Sugar 1kg',                     'pack',   4800,  4300),
  ('00000000-0000-0000-0000-00000000d001','TEA250','Tata Tea Gold 250g',            'pack',  19000, 13500)
on conflict (merchant_id, sku) do update
  set name = excluded.name, unit = excluded.unit,
      price_paise = excluded.price_paise, cost_paise = excluded.cost_paise;
