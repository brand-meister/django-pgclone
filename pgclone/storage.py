from __future__ import annotations

import abc
import importlib.util
import os
import pathlib
import subprocess
import threading
from typing import Any

from pgclone import exceptions, logging, run, settings

S3_BACKEND_BOTO3 = "boto3"
S3_BACKEND_AWSCLI = "awscli"


def _is_boto3_importable() -> bool:
    return importlib.util.find_spec("boto3") is not None


def _is_awscli_available() -> bool:
    which_aws = subprocess.run(
        "which aws", shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    return which_aws.returncode == 0


def validate_s3_support(backend: str) -> None:  # pragma: no cover
    """Verify that the configured S3 backend is available."""
    if backend == S3_BACKEND_BOTO3:
        if not _is_boto3_importable():
            raise exceptions.RuntimeError(
                "You must install boto3 to use the boto3 S3 backend."
                ' Run "pip install django-pgclone[s3]".'
            )
    elif backend == S3_BACKEND_AWSCLI:
        if not _is_awscli_available():
            raise exceptions.RuntimeError(
                "You must install the AWS command line tool in order to enable S3 support."
                ' Run "pip install awscli" or follow these instructions -'
                " https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
            )
    else:
        raise exceptions.RuntimeError(
            'Invalid PGCLONE_S3_BACKEND setting. Must be "boto3" or "awscli".'
        )


def _parse_s3_path(s3_path: str) -> tuple[str, str]:
    if not s3_path.startswith("s3://"):
        raise ValueError(f'Invalid S3 path "{s3_path}".')

    path = s3_path[5:]
    bucket, _, key = path.partition("/")
    return bucket, key


def _log_stream(stream: Any) -> None:
    logger = logging.get_logger()
    for line in iter(stream.readline, b""):
        logger.info(line.decode("utf-8").rstrip())


class _Storage(abc.ABC):
    def __init__(self, storage_location: str) -> None:
        # Ensure the storage location always has a slash appended
        self.storage_location = os.path.join(storage_location, "")
        self.env = self.get_env()

    def get_env(self) -> dict[str, Any]:
        return {}

    def dump_key(self, path: str) -> str:
        """
        Given an absolute path, return the relative path (i.e. the dump key)
        """
        prefix_len = len(self.storage_location)
        return path[prefix_len:]

    @abc.abstractmethod
    def pg_dump(self, file_path: str) -> str:
        """Given a file path, generates the CLI fragment to append to pg_dump"""
        pass

    @abc.abstractmethod
    def pg_restore(self, file_path: str) -> str:
        """Given a file path, generates the CLI fragment to prepend to pg_restore"""
        pass

    def run_pg_dump(self, pg_dump_cmd: str, file_path: str) -> None:
        cmd = pg_dump_cmd + " " + self.pg_dump(file_path)
        run.shell(cmd, env=self.env, pipefail=True)

    def run_pg_restore(self, pg_restore_cmd: str, file_path: str) -> None:
        cmd = self.pg_restore(file_path) + " " + pg_restore_cmd
        run.shell(cmd, env=self.env, ignore_errors=True)

    @abc.abstractmethod
    def ls(self, prefix: str | None = None) -> list[str]:
        """Given a prefix, returns a list of dump keys"""
        pass


class S3Awscli(_Storage):
    def __init__(self, storage_location: str):
        validate_s3_support(S3_BACKEND_AWSCLI)
        self.s3_endpoint_url = (
            f" --endpoint-url {settings.s3_endpoint_url()}"
            if settings.s3_endpoint_url() is not None
            and isinstance(settings.s3_endpoint_url(), str)
            else ""
        )
        super().__init__(storage_location)

    def ls(self, prefix: str | None = None) -> list[str]:  # pragma: no cover
        s3_path = os.path.join(self.storage_location, prefix or "")
        s3_bucket = "s3://" + s3_path[5:].split("/", 1)[0]
        cmd = f"aws s3 ls {s3_path}{self.s3_endpoint_url} --recursive | cut -c32-"
        process = subprocess.run(
            cmd, shell=True, stdout=subprocess.PIPE, check=True, env=dict(os.environ, **self.env)
        )
        abs_paths = [
            os.path.join(s3_bucket, path)
            for path in process.stdout.decode("utf-8").split("\n")
            if path
        ]
        return [self.dump_key(path) for path in abs_paths]

    def get_env(self):
        # Since AWS CLI v2.23, uploads default to sending an additional CRC32
        # integrity checksum using "aws-chunked" streaming trailers. Many
        # S3-compatible providers reject this with a "XAmzContentSHA256Mismatch"
        # error, so restore the pre-2.23 behavior of only checksumming when
        # required. This mirrors the boto3 backend's client config and is fully
        # supported by AWS S3 as well.
        return {
            "AWS_REQUEST_CHECKSUM_CALCULATION": "when_required",
            "AWS_RESPONSE_CHECKSUM_VALIDATION": "when_required",
            **settings.s3_config(),
        }

    def pg_dump(self, file_path: str) -> str:
        return f"| aws s3 cp - {file_path}{self.s3_endpoint_url}"

    def pg_restore(self, file_path: str) -> str:
        return f"aws s3 cp {file_path} -{self.s3_endpoint_url} |"


S3 = S3Awscli


class S3Boto3(_Storage):
    def __init__(self, storage_location: str):
        validate_s3_support(S3_BACKEND_BOTO3)
        self._s3_client: Any = None
        super().__init__(storage_location)

    @property
    def s3_client(self) -> Any:
        if self._s3_client is None:
            from boto3.session import Session

            session_kwargs = self._boto3_session_kwargs()
            client_kwargs = self._boto3_client_kwargs()
            session = Session(**session_kwargs)
            self._s3_client = session.client("s3", **client_kwargs)
        return self._s3_client

    def _boto3_session_kwargs(self) -> dict[str, Any]:
        config = settings.s3_config()
        kwargs: dict[str, Any] = {}
        key_mapping = {
            "AWS_ACCESS_KEY_ID": "aws_access_key_id",
            "AWS_SECRET_ACCESS_KEY": "aws_secret_access_key",
            "AWS_SESSION_TOKEN": "aws_session_token",
            "AWS_DEFAULT_REGION": "region_name",
        }
        for env_key, boto_key in key_mapping.items():
            if env_key in config and config[env_key] is not None:
                kwargs[boto_key] = config[env_key]
        return kwargs

    def _boto3_client_kwargs(self) -> dict[str, Any]:
        from botocore.config import Config

        kwargs: dict[str, Any] = {}
        endpoint_url = settings.s3_endpoint_url()
        if endpoint_url is not None and isinstance(endpoint_url, str):
            kwargs["endpoint_url"] = endpoint_url

        # Since botocore 1.36, uploads default to sending an additional CRC32
        # integrity checksum using "aws-chunked" streaming trailers. Many
        # S3-compatible providers reject this with a "XAmzContentSHA256Mismatch"
        # error, so restore the pre-1.36 behavior of only checksumming when
        # required. This is fully supported by AWS S3 as well.
        kwargs["config"] = Config(
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        )
        return kwargs

    def pg_dump(self, file_path: str) -> str:
        raise NotImplementedError

    def pg_restore(self, file_path: str) -> str:
        raise NotImplementedError

    def run_pg_dump(self, pg_dump_cmd: str, file_path: str) -> None:
        bucket, key = _parse_s3_path(file_path)
        process = subprocess.Popen(
            pg_dump_cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.stdout is None or process.stderr is None:
            raise AssertionError

        stderr_thread = threading.Thread(
            target=_log_stream,
            args=(process.stderr,),
            daemon=True,
        )
        stderr_thread.start()

        try:
            self.s3_client.upload_fileobj(process.stdout, bucket, key)
        except Exception as exc:
            process.kill()
            process.wait()
            stderr_thread.join()
            raise exceptions.RuntimeError(f"Error uploading dump to S3: {exc}") from exc
        finally:
            process.stdout.close()

        process.wait()
        stderr_thread.join()

        if process.returncode:
            try:
                self.s3_client.delete_object(Bucket=bucket, Key=key)
            except Exception:
                pass
            raise exceptions.RuntimeError("Error running command.")
    def run_pg_restore(self, pg_restore_cmd: str, file_path: str) -> None:
        bucket, key = _parse_s3_path(file_path)
        process = subprocess.Popen(
            pg_restore_cmd,
            shell=True,
            stdin=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdout=subprocess.PIPE,
        )
        if process.stdin is None or process.stdout is None:
            raise AssertionError

        output_thread = threading.Thread(
            target=_log_stream,
            args=(process.stdout,),
            daemon=True,
        )
        output_thread.start()

        try:
            self.s3_client.download_fileobj(bucket, key, process.stdin)
        except Exception as exc:
            process.kill()
            process.wait()
            output_thread.join()
            raise exceptions.RuntimeError(f"Error downloading dump from S3: {exc}") from exc
        finally:
            process.stdin.close()

        process.wait()
        output_thread.join()

    def ls(self, prefix: str | None = None) -> list[str]:
        bucket, base_prefix = _parse_s3_path(self.storage_location)
        full_prefix = f"{base_prefix}{prefix or ''}"
        paginator = self.s3_client.get_paginator("list_objects_v2")
        abs_paths = []
        for page in paginator.paginate(Bucket=bucket, Prefix=full_prefix):
            for obj in page.get("Contents", []):
                abs_paths.append(f"s3://{bucket}/{obj['Key']}")
        return [self.dump_key(path) for path in abs_paths]


class Local(_Storage):
    def ls(self, prefix: str | None = None) -> list[str]:
        abs_paths = [
            os.path.join(dirpath, file_name)
            for dirpath, _, file_names in os.walk(self.storage_location)
            for file_name in file_names
        ]
        dump_keys = [self.dump_key(path) for path in abs_paths]

        if prefix:
            dump_keys = [dump_key for dump_key in dump_keys if dump_key.startswith(prefix)]

        return dump_keys

    def pg_dump(self, file_path: str) -> str:
        pathlib.Path(file_path).parent.mkdir(parents=True, exist_ok=True)
        return f"> {file_path}"

    def pg_restore(self, file_path: str) -> str:
        return f"cat {file_path} |"


def client(storage_location: str) -> _Storage:
    if storage_location.startswith("s3://"):  # pragma: no cover
        backend = settings.s3_backend()
        validate_s3_support(backend)
        if backend == S3_BACKEND_BOTO3:
            return S3Boto3(storage_location)
        return S3Awscli(storage_location)
    else:
        return Local(storage_location)
    else:
        return Local(storage_location)
