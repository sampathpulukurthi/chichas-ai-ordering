begin;
alter table menu_items add column if not exists unavailable_reason text;
alter table menu_items add column if not exists availability_updated_at timestamptz;
alter table menu_items add column if not exists availability_updated_by text;
create table if not exists menu_availability_events(id uuid primary key default gen_random_uuid(),menu_item_id uuid not null references menu_items(id),previous_available boolean not null,new_available boolean not null,reason text,actor text not null,changed_at timestamptz not null default now());
create index if not exists menu_availability_events_item_time_idx on menu_availability_events(menu_item_id,changed_at desc);
alter table menu_items enable row level security;
alter table menu_availability_events enable row level security;
commit;