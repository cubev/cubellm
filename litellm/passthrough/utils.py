from collections.abc import Mapping
from typing import Final
from urllib.parse import parse_qs

import httpx

from litellm.constants import PASS_THROUGH_HEADER_PREFIX
from litellm.litellm_core_utils.aws_partition import contains_aws_arn

# Headers that must not be overwritten via the x-pass- forwarding mechanism.
# Includes standard credential/auth headers and protocol-level headers that
# affect routing or message framing.
_PASS_THROUGH_PROTECTED_HEADERS: Final[frozenset[str]] = frozenset(
    {
        "authorization",
        "api-key",
        "x-api-key",
        "x-goog-api-key",
        "ocp-apim-subscription-key",
        "host",
        "content-length",
        "accept-encoding",
    }
)

# Header name prefix used to block AWS SigV4 signing headers from being overridden.
_PASS_THROUGH_PROTECTED_HEADER_PREFIXES: Final[tuple[str, ...]] = ("x-amz-",)


def _can_forward_prefixed_header(name: str, configured_names: frozenset[str]) -> bool:
    if not name.lower().startswith(PASS_THROUGH_HEADER_PREFIX):
        return False
    actual: Final = name[len(PASS_THROUGH_HEADER_PREFIX) :].lower()
    return (
        bool(actual)
        and actual not in configured_names
        and actual not in _PASS_THROUGH_PROTECTED_HEADERS
        and not any(actual.startswith(prefix) for prefix in _PASS_THROUGH_PROTECTED_HEADER_PREFIXES)
    )


class BasePassthroughUtils:
    @staticmethod
    def get_merged_query_parameters(
        existing_url: httpx.URL,
        request_query_params: Mapping[str, str | list[str]],
        default_query_params: dict[str, str | list[str]] | None = None,
    ) -> dict[str, str | list[str]]:
        # Get the existing query params from the target URL
        existing_query_string: Final = existing_url.query.decode("utf-8")
        existing_query_params: Final = parse_qs(existing_query_string)

        # parse_qs returns a dict where each value is a list, so let's flatten it
        updated_existing_query_params: Final = {k: v[0] if len(v) == 1 else v for k, v in existing_query_params.items()}

        # Start with default query params (lowest priority)
        merged_params: Final[dict[str, str | list[str]]] = {}
        if default_query_params:
            merged_params.update(default_query_params)

        # Override with existing URL query params (medium priority)
        merged_params.update(updated_existing_query_params)

        # Override with request query params (highest priority - client can override anything)
        merged_params.update(request_query_params)

        return merged_params

    @staticmethod
    def forward_headers_from_request(
        request_headers: Mapping[str, str],
        headers: Mapping[str, str],
        forward_headers: bool | None = False,
    ) -> dict[str, str]:
        configured_names: Final = frozenset(name.lower() for name in headers)
        prefixed: Final = {
            name[len(PASS_THROUGH_HEADER_PREFIX) :].lower(): value
            for name, value in request_headers.items()
            if _can_forward_prefixed_header(name, configured_names)
        }
        forwarded: Final = {
            name: value
            for name, value in request_headers.items()
            if forward_headers is True
            and name.lower() not in {"content-length", "host", "accept-encoding"}
            and name.lower() not in configured_names
            and name.lower() not in prefixed
        }
        return {**forwarded, **headers, **prefixed}


class CommonUtils:
    @staticmethod
    def encode_bedrock_runtime_modelid_arn(endpoint: str) -> str:
        """
        Encodes any "/" found in the modelId of an AWS Bedrock Runtime Endpoint when arns are passed in.
        - modelID value can be an ARN which contains slashes that SHOULD NOT be treated as path separators.
        e.g endpoint: /model/<modelId>/invoke
        <modelId> containing arns with slashes need to be encoded from
            arn:aws:bedrock:ap-southeast-1:123456789012:application-inference-profile/abdefg12334 =>
            arn:aws:bedrock:ap-southeast-1:123456789012:application-inference-profile%2Fabdefg12334
        so that it is treated as one part of the path.
        Otherwise, the encoded endpoint will return 500 error when passed to Bedrock endpoint.

        See the apis in https://docs.aws.amazon.com/bedrock/latest/APIReference/API_Operations_Amazon_Bedrock_Runtime.html
        for more details on the regex patterns of modelId which we use in the regex logic below.

        Args:
            endpoint (str): The original endpoint string which may contain ARNs that contain slashes.

        Returns:
            str: The endpoint with properly encoded ARN slashes
        """
        import re

        # Early exit: if no ARN detected, return unchanged
        if not contains_aws_arn(endpoint):
            return endpoint

        # Handle all patterns in one go - more efficient and cleaner
        patterns: Final = [
            # Custom model with 2 slashes (order matters - do this first)
            (r"(custom-model)/([a-z0-9.-]+)/([a-z0-9]+)", r"\1%2F\2%2F\3"),
            # All other resource types with 1 slash
            (r"(:application-inference-profile)/", r"\1%2F"),
            (r"(:inference-profile)/", r"\1%2F"),
            (r"(:foundation-model)/", r"\1%2F"),
            (r"(:imported-model)/", r"\1%2F"),
            (r"(:provisioned-model)/", r"\1%2F"),
            (r"(:prompt)/", r"\1%2F"),
            (r"(:endpoint)/", r"\1%2F"),
            (r"(:prompt-router)/", r"\1%2F"),
            (r"(:default-prompt-router)/", r"\1%2F"),
        ]

        for pattern, replacement in patterns:
            # Check if pattern exists before applying regex (early exit optimization)
            if re.search(pattern, endpoint):
                endpoint = re.sub(pattern, replacement, endpoint)
                break  # Exit after first match since each ARN has only one resource type

        return endpoint
