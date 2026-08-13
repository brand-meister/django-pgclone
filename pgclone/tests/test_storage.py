import io
from unittest import mock

import pytest

from pgclone import exceptions, storage


@pytest.fixture(autouse=True)
def dont_validate_s3_support(mocker):
    """None of our S3 tests use boto or awscli, so dont validate S3 support"""
    mocker.patch("pgclone.storage.validate_s3_support", autospec=True)


def test_s3_env(settings):
    settings.PGCLONE_S3_CONFIG = {
        "AWS_ACCESS_KEY_ID": "access_key",
        "AWS_SECRET_ACCESS_KEY": "secret_access_key",
        "AWS_DEFAULT_REGION": "region",
    }

    assert storage.S3Awscli("s3://bucket/").env == {
        "AWS_REQUEST_CHECKSUM_CALCULATION": "when_required",
        "AWS_RESPONSE_CHECKSUM_VALIDATION": "when_required",
        "AWS_ACCESS_KEY_ID": "access_key",
        "AWS_SECRET_ACCESS_KEY": "secret_access_key",
        "AWS_DEFAULT_REGION": "region",
    }

    delattr(settings, "PGCLONE_S3_CONFIG")

    assert storage.S3Awscli("s3://bucket/").env == {
        "AWS_REQUEST_CHECKSUM_CALCULATION": "when_required",
        "AWS_RESPONSE_CHECKSUM_VALIDATION": "when_required",
    }


def test_s3_pg_dump():
    assert storage.S3Awscli("s3://bucket/").pg_dump("s3://bucket/file_path") == (
        "| aws s3 cp - s3://bucket/file_path"
    )


def test_s3_pg_restore():
    assert storage.S3Awscli("s3://bucket/").pg_restore("s3://bucket/file_path") == (
        "aws s3 cp s3://bucket/file_path - |"
    )


def test_s3_pg_dump_with_endpoint_url(settings):
    settings.PGCLONE_S3_ENDPOINT_URL = "https://endpoint.example.com"
    assert (
        storage.S3Awscli("s3://bucket/").pg_dump("s3://bucket/file_path")
        == "| aws s3 cp - s3://bucket/file_path --endpoint-url https://endpoint.example.com"
    )


def test_s3_pg_restore_with_endpoint_url(settings):
    settings.PGCLONE_S3_ENDPOINT_URL = "https://endpoint.example.com"
    assert (
        storage.S3Awscli("s3://bucket/").pg_restore("s3://bucket/file_path")
        == "aws s3 cp s3://bucket/file_path - --endpoint-url https://endpoint.example.com |"
    )


def test_s3_alias():
    assert storage.S3 is storage.S3Awscli


def test_local_run_pg_dump(mocker):
    shell = mocker.patch("pgclone.storage.run.shell", autospec=True)
    local = storage.Local("/tmp/pgclone/")
    local.run_pg_dump("pg_dump cmd", "/tmp/pgclone/file.dump")
    shell.assert_called_once_with(
        "pg_dump cmd > /tmp/pgclone/file.dump",
        env={},
        pipefail=True,
    )


def test_local_run_pg_restore(mocker):
    shell = mocker.patch("pgclone.storage.run.shell", autospec=True)
    local = storage.Local("/tmp/pgclone/")
    local.run_pg_restore("pg_restore cmd", "/tmp/pgclone/file.dump")
    shell.assert_called_once_with(
        "cat /tmp/pgclone/file.dump | pg_restore cmd",
        env={},
        ignore_errors=True,
    )


def test_s3_awscli_run_pg_dump(mocker):
    shell = mocker.patch("pgclone.storage.run.shell", autospec=True)
    s3 = storage.S3Awscli("s3://bucket/")
    s3.run_pg_dump("pg_dump cmd", "s3://bucket/file_path")
    shell.assert_called_once_with(
        "pg_dump cmd | aws s3 cp - s3://bucket/file_path",
        env={
            "AWS_REQUEST_CHECKSUM_CALCULATION": "when_required",
            "AWS_RESPONSE_CHECKSUM_VALIDATION": "when_required",
        },
        pipefail=True,
    )


def test_s3_awscli_run_pg_restore(mocker):
    shell = mocker.patch("pgclone.storage.run.shell", autospec=True)
    s3 = storage.S3Awscli("s3://bucket/")
    s3.run_pg_restore("pg_restore cmd", "s3://bucket/file_path")
    shell.assert_called_once_with(
        "aws s3 cp s3://bucket/file_path - | pg_restore cmd",
        env={
            "AWS_REQUEST_CHECKSUM_CALCULATION": "when_required",
            "AWS_RESPONSE_CHECKSUM_VALIDATION": "when_required",
        },
        ignore_errors=True,
    )


def test_client_defaults_to_awscli_backend(mocker, settings):
    delattr(settings, "PGCLONE_S3_BACKEND")
    mocker.patch("importlib.util.find_spec", return_value=mock.Mock())
    client = storage.client("s3://bucket/")
    assert isinstance(client, storage.S3Awscli)


def test_client_uses_boto3_backend(mocker):
    mocker.patch("pgclone.settings.s3_backend", return_value="boto3")
    client = storage.client("s3://bucket/")
    assert isinstance(client, storage.S3Boto3)


def test_client_uses_awscli_backend(mocker):
    mocker.patch("pgclone.settings.s3_backend", return_value="awscli")
    client = storage.client("s3://bucket/")
    assert isinstance(client, storage.S3Awscli)


def test_client_uses_local_backend():
    client = storage.client("/tmp/pgclone/")
    assert isinstance(client, storage.Local)


def test_parse_s3_path():
    assert storage._parse_s3_path("s3://bucket/key/path") == ("bucket", "key/path")
    with pytest.raises(ValueError):
        storage._parse_s3_path("/local/path")


@pytest.fixture
def boto3_client(mocker):
    mock_boto3 = mocker.patch("boto3.session.Session", autospec=True)
    mock_session = mock_boto3.return_value
    mock_client = mock_session.client.return_value
    return mock_client


def test_s3_boto3_session_kwargs(settings, mocker):
    settings.PGCLONE_S3_CONFIG = {
        "AWS_ACCESS_KEY_ID": "access_key",
        "AWS_SECRET_ACCESS_KEY": "secret_access_key",
        "AWS_SESSION_TOKEN": "session_token",
        "AWS_DEFAULT_REGION": "us-east-1",
    }
    mock_session_cls = mocker.patch("boto3.session.Session", autospec=True)
    s3 = storage.S3Boto3("s3://bucket/")
    assert s3.s3_client is mock_session_cls.return_value.client.return_value
    mock_session_cls.assert_called_once_with(
        aws_access_key_id="access_key",
        aws_secret_access_key="secret_access_key",
        aws_session_token="session_token",
        region_name="us-east-1",
    )
    mock_session_cls.return_value.client.assert_called_once_with("s3", config=mock.ANY)


def test_s3_boto3_client_kwargs(settings, mocker):
    settings.PGCLONE_S3_ENDPOINT_URL = "https://endpoint.example.com"
    mock_session_cls = mocker.patch("boto3.session.Session", autospec=True)
    s3 = storage.S3Boto3("s3://bucket/")
    client = s3.s3_client
    mock_session_cls.return_value.client.assert_called_once_with(
        "s3",
        endpoint_url="https://endpoint.example.com",
        config=mock.ANY,
    )
    assert client is mock_session_cls.return_value.client.return_value


def test_s3_boto3_ls(boto3_client):
    paginator = boto3_client.get_paginator.return_value
    paginator.paginate.return_value = [
        {
            "Contents": [
                {"Key": "prefix/instance/db/config/2024-01-01-00-00-00-000000.dump"},
                {"Key": "prefix/other.dump"},
            ]
        }
    ]
    s3 = storage.S3Boto3("s3://bucket/prefix/")
    dump_keys = s3.ls()
    paginator.paginate.assert_called_once_with(Bucket="bucket", Prefix="prefix/")
    assert dump_keys == [
        "instance/db/config/2024-01-01-00-00-00-000000.dump",
        "other.dump",
    ]


def test_s3_boto3_ls_with_prefix(boto3_client):
    paginator = boto3_client.get_paginator.return_value
    paginator.paginate.return_value = [{"Contents": []}]
    s3 = storage.S3Boto3("s3://bucket/prefix/")
    s3.ls(prefix="instance/")
    paginator.paginate.assert_called_once_with(Bucket="bucket", Prefix="prefix/instance/")


def test_s3_boto3_run_pg_dump_success(mocker, boto3_client):
    process = mock.Mock()
    process.stdout = io.BytesIO(b"dump-data")
    process.stderr = io.BytesIO(b"")
    process.returncode = 0
    process.wait.return_value = 0
    mocker.patch("pgclone.storage.subprocess.Popen", return_value=process)

    s3 = storage.S3Boto3("s3://bucket/")
    s3.run_pg_dump("pg_dump cmd", "s3://bucket/key.dump")

    boto3_client.upload_fileobj.assert_called_once_with(process.stdout, "bucket", "key.dump")


def test_s3_boto3_run_pg_dump_upload_failure(mocker, boto3_client):
    process = mock.Mock()
    process.stdout = io.BytesIO(b"dump-data")
    process.stderr = io.BytesIO(b"")
    mocker.patch("pgclone.storage.subprocess.Popen", return_value=process)
    boto3_client.upload_fileobj.side_effect = Exception("upload failed")

    s3 = storage.S3Boto3("s3://bucket/")
    with pytest.raises(exceptions.RuntimeError, match="Error uploading dump to S3"):
        s3.run_pg_dump("pg_dump cmd", "s3://bucket/key.dump")

    process.kill.assert_called_once()


def test_s3_boto3_run_pg_dump_process_failure(mocker, boto3_client):
    process = mock.Mock()
    process.stdout = io.BytesIO(b"dump-data")
    process.stderr = io.BytesIO(b"")
    process.returncode = 1
    process.wait.return_value = 1
    mocker.patch("pgclone.storage.subprocess.Popen", return_value=process)

    s3 = storage.S3Boto3("s3://bucket/")
    with pytest.raises(exceptions.RuntimeError, match="Error running command"):
        s3.run_pg_dump("pg_dump cmd", "s3://bucket/key.dump")


def test_s3_boto3_run_pg_restore_success(mocker, boto3_client):
    process = mock.Mock()
    process.stdin = mock.Mock()
    process.stdout = io.BytesIO(b"")
    process.returncode = 0
    process.wait.return_value = 0
    mocker.patch("pgclone.storage.subprocess.Popen", return_value=process)

    s3 = storage.S3Boto3("s3://bucket/")
    s3.run_pg_restore("pg_restore cmd", "s3://bucket/key.dump")

    boto3_client.download_fileobj.assert_called_once_with("bucket", "key.dump", process.stdin)


def test_s3_boto3_run_pg_restore_download_failure(mocker, boto3_client):
    process = mock.Mock()
    process.stdin = mock.Mock()
    process.stdout = io.BytesIO(b"")
    mocker.patch("pgclone.storage.subprocess.Popen", return_value=process)
    boto3_client.download_fileobj.side_effect = Exception("download failed")

    s3 = storage.S3Boto3("s3://bucket/")
    with pytest.raises(exceptions.RuntimeError, match="Error downloading dump from S3"):
        s3.run_pg_restore("pg_restore cmd", "s3://bucket/key.dump")

    process.kill.assert_called_once()


def test_s3_boto3_run_pg_restore_ignores_pg_restore_errors(mocker, boto3_client):
    process = mock.Mock()
    process.stdin = mock.Mock()
    process.stdout = io.BytesIO(b"")
    process.returncode = 1
    process.wait.return_value = 1
    mocker.patch("pgclone.storage.subprocess.Popen", return_value=process)

    s3 = storage.S3Boto3("s3://bucket/")
    s3.run_pg_restore("pg_restore cmd", "s3://bucket/key.dump")


def test_log_stream(mocker):
    logger = mocker.patch("pgclone.storage.logging.get_logger", autospec=True)
    stream = io.BytesIO(b"line1\nline2\n")
    storage._log_stream(stream)
    assert logger.return_value.info.call_count == 2


def test_is_boto3_importable(mocker):
    mocker.patch("importlib.util.find_spec", return_value=mock.Mock())
    assert storage._is_boto3_importable() is True
    mocker.patch("importlib.util.find_spec", return_value=None)
    assert storage._is_boto3_importable() is False


def test_is_awscli_available(mocker):
    mocker.patch(
        "pgclone.storage.subprocess.run",
        return_value=mock.Mock(returncode=0),
    )
    assert storage._is_awscli_available() is True
    mocker.patch(
        "pgclone.storage.subprocess.run",
        return_value=mock.Mock(returncode=1),
    )
    assert storage._is_awscli_available() is False


def test_s3_boto3_client_cached(mocker):
    mock_session_cls = mocker.patch("boto3.session.Session", autospec=True)
    s3 = storage.S3Boto3("s3://bucket/")
    first_client = s3.s3_client
    second_client = s3.s3_client
    mock_session_cls.assert_called_once()
    assert first_client is second_client


def test_local_ls(tmp_path):
    dump_dir = tmp_path / "pgclone" / "instance" / "db" / "config"
    dump_dir.mkdir(parents=True)
    dump_file = dump_dir / "2024-01-01-00-00-00-000000.dump"
    dump_file.write_text("dump", encoding="utf-8")

    local = storage.Local(str(tmp_path / "pgclone") + "/")
    dump_keys = local.ls()
    assert dump_keys == ["instance/db/config/2024-01-01-00-00-00-000000.dump"]

    assert local.ls(prefix="instance/") == ["instance/db/config/2024-01-01-00-00-00-000000.dump"]


def test_local_pg_dump(tmp_path):
    local = storage.Local(str(tmp_path / "pgclone") + "/")
    file_path = str(tmp_path / "pgclone" / "file.dump")
    assert local.pg_dump(file_path) == f"> {file_path}"
    assert (tmp_path / "pgclone").exists()


def test_local_pg_restore():
    local = storage.Local("/tmp/pgclone/")
    assert local.pg_restore("/tmp/pgclone/file.dump") == "cat /tmp/pgclone/file.dump |"
