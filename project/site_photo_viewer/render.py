from __future__ import annotations

import html
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlencode

from .read_model import (
    INVALID_REFERENCE,
    MEDIA_AVAILABLE,
    MEDIA_UNAVAILABLE,
    VISION_AVAILABLE,
    SitePhotoPage,
    TokenSiteScope,
)


def render_site_picker(scope: TokenSiteScope, token: str) -> str:
    items = []
    for site_id in scope.allowed_site_ids:
        href = viewer_url(token, site_id=site_id)
        items.append(
            f'<li><a href="{_escape(href)}">{_escape(scope.site_labels[site_id])}</a></li>'
        )
    body = (
        '<section class="site-picker" aria-labelledby="site-picker-heading">'
        '<h1 id="site-picker-heading">Choose a site</h1>'
        f'<ul class="site-list">{"".join(items)}</ul></section>'
    )
    return html_document("Choose a site", body)


def render_latest_page(
    scope: TokenSiteScope,
    token: str,
    page: SitePhotoPage,
    *,
    query: str = "",
) -> str:
    assert scope.selected_site_id is not None
    site_id = scope.selected_site_id
    site_label = scope.site_labels[site_id]
    picker_link = ""
    if len(scope.allowed_site_ids) > 1:
        picker_link = (
            '<a class="site-picker-link" '
            f'href="{_escape(viewer_url(token))}">Choose another site</a>'
        )

    hidden_site = (
        f'<input type="hidden" name="site" value="{_escape(site_id)}">'
        if len(scope.allowed_site_ids) > 1
        else ""
    )
    search_form = (
        f'<form class="search-form" method="get" action="{_escape(viewer_url(token))}">'
        f'<input type="hidden" name="token" value="{_escape(token)}">'
        f"{hidden_site}"
        '<label for="photo-search">Search photos</label>'
        '<div class="search-controls">'
        f'<input id="photo-search" name="q" type="search" maxlength="200" value="{_escape(query)}">'
        '<button type="submit">Search</button>'
        "</div></form>"
    )

    groups = "".join(_render_group(group, token) for group in page.groups)
    if groups:
        gallery = f'<div class="date-groups">{groups}</div>'
    elif query.strip():
        gallery = '<p class="empty-state">No photos match your search.</p>'
    else:
        gallery = '<p class="empty-state">No photos are available.</p>'

    result_count = (
        f'<p class="result-count">Showing {page.first_position}–{page.last_position} '
        f"of {page.total_results} photos</p>"
    )
    pagination = _render_pagination(page)
    body = (
        '<header class="page-header">'
        f'<div><h1>{_escape(site_label)}</h1><h2>Latest photos</h2></div>{picker_link}'
        "</header>"
        f"{search_form}{result_count}{gallery}{pagination}"
    )
    return html_document(f"Latest photos — {site_label}", body)


def _render_group(group: Any, token: str) -> str:
    if group.label == "Date unavailable":
        heading = _escape(group.label)
    else:
        heading = f'<time datetime="{_escape(group.label)}">{_escape(group.label)}</time>'
    photos = "".join(_render_photo(photo, token) for photo in group.photos)
    return (
        '<section class="date-group">'
        f'<h3 class="date-heading">{heading}</h3>'
        f'<div class="photo-grid">{photos}</div></section>'
    )


def _render_photo(photo: Any, token: str) -> str:
    alt_text = photo.summary or photo.filename or "Site photo"
    if photo.media_key and photo.availability_state == MEDIA_AVAILABLE:
        href = media_url(photo.media_key, token)
        media = (
            f'<a class="media-frame" href="{_escape(href)}">'
            f'<img src="{_escape(href)}" alt="{_escape(alt_text)}" loading="lazy">'
            "</a>"
        )
    else:
        unavailable_message = _media_unavailable_message(photo.availability_state)
        media = (
            '<div class="media-frame media-placeholder" role="img" '
            f'aria-label="{_escape(unavailable_message)}">'
            f'<p>{_escape(unavailable_message)}</p></div>'
        )

    metadata = (
        '<p class="photo-meta">'
        f"{_render_captured_time(photo.captured_at)}"
        '<span class="metadata-separator" aria-hidden="true">·</span>'
        f'<span>Submitted category: {_escape(photo.capture_category or "Not provided")}</span>'
        "</p>"
    )
    if photo.vision_state == VISION_AVAILABLE:
        summary = photo.summary or "Vision summary is not available."
        vision = f'<p class="vision-summary">{_escape(summary)}</p>{_render_details(photo)}'
    else:
        vision = '<p class="vision-summary">Vision analysis is not available.</p>'

    return (
        '<figure class="photo-item">'
        f'{media}<figcaption class="photo-copy">{metadata}{vision}</figcaption>'
        "</figure>"
    )


def _render_details(photo: Any) -> str:
    return (
        '<details class="photo-details"><summary>Photo details</summary><dl>'
        '<div><dt>Description</dt>'
        f'<dd>{_escape(photo.description or "Not provided")}</dd></div>'
        '<div><dt>Area</dt>'
        f'<dd>{_escape(photo.area_guess or "Not provided")}</dd></div>'
        '<div><dt>Vision QC category</dt>'
        f'<dd>{_escape(photo.qc_category or "Not provided")}</dd></div>'
        "</dl></details>"
    )


def _render_captured_time(value: str) -> str:
    escaped_value = _escape(value)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return "<span>Captured time unavailable</span>"
    display = parsed.strftime("%I:%M %p").lstrip("0")
    return f'<time datetime="{escaped_value}">Captured {display}</time>'


def _render_pagination(page: SitePhotoPage) -> str:
    links = []
    if page.previous_url:
        links.append(f'<a rel="prev" href="{_escape(page.previous_url)}">Previous</a>')
    if page.next_url:
        links.append(f'<a rel="next" href="{_escape(page.next_url)}">Next</a>')
    if not links:
        return ""
    return f'<nav class="pagination" aria-label="Photo pages">{"".join(links)}</nav>'


def _media_unavailable_message(state: str) -> str:
    if state == INVALID_REFERENCE:
        return "Media reference is invalid."
    if state == MEDIA_UNAVAILABLE:
        return "Media is unavailable in durable storage."
    return "Media availability could not be checked."


def html_document(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow,noarchive">'
        '<meta name="color-scheme" content="light dark">'
        f'<title>{_escape(title)}</title><link rel="stylesheet" href="/viewer.css">'
        '<script src="/viewer.js" defer></script>'
        f"</head><body><main>{body}</main></body></html>"
    )


def viewer_url(
    token: str,
    *,
    site_id: str | None = None,
    query: str = "",
    page_number: int | None = None,
) -> str:
    values: list[tuple[str, str]] = [("token", token)]
    if site_id is not None:
        values.append(("site", site_id))
    if query:
        values.append(("q", query))
    if page_number is not None and page_number > 1:
        values.append(("page", str(page_number)))
    return f"/?{urlencode(values)}"


def media_url(media_key: str, token: str) -> str:
    return f"/media/{quote(media_key, safe='')}?{urlencode({'token': token})}"


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)
