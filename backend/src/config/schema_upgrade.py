from sqlalchemy import inspect,text
from .database import Base

def ensure_schema(engine):
    """Create missing tables, then add the nullable policy column to pre-upgrade run tables.

    Existing rows keep NULL; the API/worker resolve NULL to the strict reject policy.
    """
    Base.metadata.create_all(engine)
    inspector=inspect(engine)
    if 'deviation_runs' not in inspector.get_table_names():return
    columns={c['name'] for c in inspector.get_columns('deviation_runs')}
    if 'missing_policy' not in columns:
        with engine.begin() as connection:connection.execute(text('ALTER TABLE deviation_runs ADD COLUMN missing_policy VARCHAR'))
