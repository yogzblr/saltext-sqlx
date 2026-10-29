import saltext.sqlx.modules.sqlx_mod as sqlx_module


def test_virtual():
    assert sqlx_module.__virtual__() == "sqlx"
