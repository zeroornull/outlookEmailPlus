"""Refresh-token transport with Microsoft's explicit public-client correction."""

import requests


def post_refresh_token(url: str, *, data: dict, **kwargs) -> requests.Response:
    """Retry once without a secret only when Microsoft explicitly rejects it."""
    response = requests.post(url, data=data, **kwargs)
    if response.status_code != 400 or not data.get("client_secret"):
        return response

    try:
        error = response.json()
    except ValueError:
        return response
    if not isinstance(error, dict) or error.get("error") != "invalid_request":
        return response

    description = error.get("error_description")
    if not isinstance(description, str):
        return response
    description = description.lower()
    codes = error.get("error_codes")
    has_public_client_code = isinstance(codes, list) and any(code in (90023, "90023") for code in codes)
    if not has_public_client_code and "aadsts90023:" not in description:
        return response
    if "public clients can't send a client secret." not in description:
        return response

    public_data = data.copy()
    del public_data["client_secret"]
    return requests.post(url, data=public_data, **kwargs)
