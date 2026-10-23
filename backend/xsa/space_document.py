"""Build a human-readable, portable information page for a recorded Space."""

from __future__ import annotations

from html import escape
from pathlib import Path
from urllib.parse import quote


def write_space_info_html(folder: Path, metadata: dict) -> Path:
    """Write space-info.html with a local cover and a direct X link."""
    space_id = str(metadata.get("space_id") or folder.name)
    title = str(metadata.get("title") or "مساحة بدون عنوان")
    host = str(metadata.get("host_display_name") or metadata.get("host") or "مضيف غير مسجل")
    username = str(metadata.get("host") or "").strip().lstrip("@")
    url = str(metadata.get("url") or f"https://x.com/i/spaces/{space_id}")

    image_path = None
    for candidate in (
        folder / str(metadata.get("icon_path") or ""),
        folder / "thumbnail.jpg",
    ):
        if candidate.is_file():
            image_path = candidate
            break
    if image_path is None:
        screenshots = sorted((folder / "screenshots").glob("join_*.png"), reverse=True)
        if screenshots:
            image_path = screenshots[0]

    image_html = ""
    if image_path:
        relative = quote(image_path.relative_to(folder).as_posix(), safe="/")
        image_html = (
            f'<img class="cover" src="{escape(relative, quote=True)}" '
            f'alt="معاينة مساحة {escape(title, quote=True)}">'
        )
    avatar_url = str(metadata.get("host_avatar_url") or "")
    avatar_html = (
        f'<img class="avatar" src="{escape(avatar_url, quote=True)}" alt="صورة {escape(host, quote=True)}">'
        if avatar_url
        else '<div class="avatar fallback" aria-hidden="true">◉</div>'
    )
    safe_title = escape(title)
    safe_host = escape(host)
    safe_username = escape(f"@{username}" if username else "")
    safe_id = escape(space_id)
    safe_url = escape(url, quote=True)
    started = escape(str(metadata.get("started_at_local") or metadata.get("started_at") or "غير متوفر"))

    html = f'''<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{safe_title} — معلومات المساحة</title>
  <style>
    :root {{ color-scheme: dark; font-family: "Segoe UI", Tahoma, sans-serif; }}
    body {{ margin: 0; padding: 32px 16px; background: #080b12; color: #f4f7fb; }}
    main {{ max-width: 760px; margin: auto; overflow: hidden; border: 1px solid #293244;
      border-radius: 20px; background: #111722; box-shadow: 0 18px 55px #0008; }}
    .cover {{ display: block; width: 100%; max-height: 360px; object-fit: cover; background: #080b12; }}
    .content {{ padding: 24px; }}
    h1 {{ margin: 0 0 20px; font-size: clamp(22px, 4vw, 34px); line-height: 1.45; }}
    .host {{ display: flex; align-items: center; gap: 12px; margin: 18px 0 24px; }}
    .avatar {{ width: 52px; height: 52px; border-radius: 50%; object-fit: cover;
      border: 2px solid #34445e; }}
    .fallback {{ display: grid; place-items: center; background: #202b3b; color: #43b7ff; font-size: 26px; }}
    .muted {{ color: #9ba9bb; font-size: 13px; }}
    dl {{ display: grid; grid-template-columns: max-content 1fr; gap: 12px 20px;
      padding: 18px 0; margin: 0; border-top: 1px solid #293244; }}
    dt {{ color: #9ba9bb; }} dd {{ margin: 0; overflow-wrap: anywhere; }}
    a {{ display: inline-block; margin-top: 8px; padding: 11px 18px; border-radius: 10px;
      background: #168de0; color: white; text-decoration: none; font-weight: 700; }}
    a:hover {{ background: #0877c5; }}
  </style>
</head>
<body>
  <main>
    {image_html}
    <section class="content">
      <h1>{safe_title}</h1>
      <div class="host">{avatar_html}<div><strong>{safe_host}</strong>
        <div class="muted">{safe_username}</div></div></div>
      <dl>
        <dt>معرّف المساحة</dt><dd>{safe_id}</dd>
        <dt>وقت بدء التسجيل</dt><dd>{started}</dd>
      </dl>
      <a href="{safe_url}" target="_blank" rel="noopener noreferrer">فتح المساحة على X</a>
    </section>
  </main>
</body>
</html>
'''
    output = folder / "space-info.html"
    temp = output.with_suffix(".html.tmp")
    temp.write_text(html, encoding="utf-8")
    temp.replace(output)
    return output
