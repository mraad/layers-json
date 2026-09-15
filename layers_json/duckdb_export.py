"""Shared Arrow conversion and DuckDB batch writes; no ArcPy dependency."""

import datetime as dt


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def arrow_batch(rows, schema, pa):
    arrays = []
    for i, field in enumerate(schema):
        values = [row[i] for row in rows]
        if pa.types.is_string(field.type):
            values = [None if v is None else str(v) for v in values]
        elif pa.types.is_timestamp(field.type):
            values = [
                dt.datetime.combine(v, dt.time())
                if isinstance(v, dt.date) and not isinstance(v, dt.datetime)
                else v
                for v in values
            ]
        elif pa.types.is_time(field.type):
            values = [v.time() if isinstance(v, dt.datetime) else v for v in values]
        arrays.append(pa.array(values, type=field.type))
    return pa.Table.from_arrays(arrays, schema=schema)


def write_batch(conn, table, shape, batch, create, *, replace=False):
    conn.register("_aprx_batch", batch)
    try:
        command = "CREATE OR REPLACE TABLE" if replace else "CREATE TABLE"
        prefix = f"{command} {quote(table)} AS" if create else f"INSERT INTO {quote(table)}"
        selection = (
            f"* EXCLUDE ({quote(shape)}), ST_GeomFromWKB({quote(shape)}) AS geometry"
            if shape
            else "*"
        )
        conn.execute(f"{prefix} SELECT {selection} FROM _aprx_batch")
    finally:
        conn.unregister("_aprx_batch")


def arrow_types(pa):
    """ArcGIS attribute types supported by both DuckDB exporters."""
    return {
        "SmallInteger": pa.int16(),
        "Integer": pa.int32(),
        "BigInteger": pa.int64(),
        "OID": pa.int64(),
        "Single": pa.float32(),
        "Double": pa.float64(),
        "String": pa.string(),
        "GUID": pa.string(),
        "GlobalID": pa.string(),
        "XML": pa.string(),
        "Blob": pa.binary(),
        "Date": pa.timestamp("us"),
        "DateOnly": pa.date32(),
        "TimeOnly": pa.time64("us"),
        "TimestampOffset": pa.timestamp("us", tz="UTC"),
    }


def write_sp_ref(conn, wkid, text):
    """Record the export spatial reference beside the layer tables."""
    conn.execute("CREATE OR REPLACE TABLE sp_ref (wkid INTEGER, text VARCHAR)")
    conn.execute("INSERT INTO sp_ref VALUES (?, ?)", [wkid, text])


def create_indices(conn, table, oid, has_geometry):
    """Add the object-ID key and spatial index after all batches load."""
    if oid:
        conn.execute(f"ALTER TABLE {quote(table)} ADD PRIMARY KEY ({quote(oid)});")
    if has_geometry:
        conn.execute(
            f"CREATE INDEX {quote(table + '_rtree')} ON {quote(table)} USING RTREE (geometry);"
        )
