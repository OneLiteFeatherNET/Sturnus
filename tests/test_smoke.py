import sturnus


def test_package_exposes_version() -> None:
    assert isinstance(sturnus.__version__, str)
    assert sturnus.__version__.count(".") == 2
