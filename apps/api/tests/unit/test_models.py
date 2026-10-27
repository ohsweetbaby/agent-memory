"""模型层纯单元测试：不依赖数据库。"""

from __future__ import annotations

import uuid

from app.models import Base, Memory, new_uuid7


def test_new_uuid7_is_rfc_9562_version_7() -> None:
    value = new_uuid7()
    parsed = uuid.UUID(value)
    assert str(parsed) == value
    assert parsed.version == 7
    assert parsed.variant == uuid.RFC_4122


def test_all_tables_use_uuid_string_primary_key() -> None:
    for table in Base.metadata.tables.values():
        primary_key = list(table.primary_key.columns)
        assert len(primary_key) == 1, table.name
        column = primary_key[0]
        assert column.name == "id", table.name
        assert column.type.length == 36, table.name


def test_metadata_attribute_maps_to_reserved_column_name() -> None:
    assert "metadata_" in Memory.__mapper__.attrs
    assert Memory.__table__.c.metadata.name == "metadata"
    assert "metadata" in Memory.__table__.c.keys()
