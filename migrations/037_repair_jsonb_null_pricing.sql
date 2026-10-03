-- Repair provider_models.pricing rows holding the jsonb *value* null.
--
-- The save path wrote json.dumps({}) -> the string "null" for a mapping whose
-- pricing was omitted, and 'null'::jsonb is a valid jsonb value rather than SQL
-- NULL. The snapshot read decodes jsonb with no codec, so json.loads turned that
-- value into None, and SnapshotProviderModel.pricing is a plain dict: validation
-- failed with "provider_models.N.pricing: Input should be a valid dictionary" and
-- publish_config returned 422 for the whole configuration. One unpriced row on
-- provider `ozdoev` blocked publishing, because the guided provider form never
-- displayed a rate for it and so re-sent the empty value on every save.
--
-- {} is already the documented meaning of "no rate configured" and is what the
-- routing and accounting paths expect (routing.engine treats a non-dict or empty
-- pricing as unpriced). Only the json-null value is rewritten: a row holding a
-- real object, and a row holding SQL NULL, are both already correct and are left
-- alone. The writer no longer produces this value (see the coalesce guards in
-- admin/control_plane.py and admin/reconcile.py), so this is a one-time repair.

begin;

do $$
declare
  repaired integer;
begin
  update public.provider_models
     set pricing = '{}'::jsonb,
         updated_at = now()
   where pricing is not null
     and jsonb_typeof(pricing) = 'null';

  get diagnostics repaired = row_count;
  if repaired > 0 then
    raise notice 'repaired % provider_models row(s) holding jsonb null pricing', repaired;
  end if;
end;
$$;

commit;
