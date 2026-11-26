from patchpost.formatting import (
    html_to_markdown,
    looks_like_patch,
    steam_bbcode_to_markdown,
    truncate,
)


def test_steam_bbcode_basics():
    src = (
        "[h2]Patch 1.2.3[/h2][p]Hallo [b]Welt[/b] und [i]so[/i].[/p]"
        "[list][*]Fix A[*]Fix B[/list]"
        '[url="https://example.com"]Mehr Infos[/url]'
        "[img]{STEAM_CLAN_IMAGE}/123/abc.png[/img]"
    )
    md, images = steam_bbcode_to_markdown(src)
    assert "**Patch 1.2.3**" in md
    assert "Hallo **Welt** und *so*." in md
    assert "• Fix A" in md and "• Fix B" in md
    assert "[Mehr Infos](https://example.com)" in md
    assert images == ["https://clan.akamai.steamstatic.com/images/123/abc.png"]
    assert "[" not in md.replace("[Mehr Infos]", "")


def test_steam_new_img_and_youtube():
    src = '[img src="{STEAM_CLAN_IMAGE}/1/x.jpg"][/img][previewyoutube="dQw4w9WgXcQ;full"][/previewyoutube]'
    md, images = steam_bbcode_to_markdown(src)
    assert images == ["https://clan.akamai.steamstatic.com/images/1/x.jpg"]
    assert "https://youtu.be/dQw4w9WgXcQ" in md


def test_html_to_markdown():
    src = '<h2>Neu</h2><p>Ein <strong>Test</strong> <a href="https://a.b/c">Link</a></p><ul><li>Eins</li></ul><img src="https://a.b/i.png">'
    md, images = html_to_markdown(src)
    assert "**Neu**" in md
    assert "Ein **Test** [Link](https://a.b/c)" in md
    assert "• Eins" in md
    assert images == ["https://a.b/i.png"]


def test_patch_detection():
    assert looks_like_patch("Patch Notes 1.4")
    assert looks_like_patch("Hotfix for crashes")
    assert looks_like_patch("Version 0.9.2b is live")
    assert not looks_like_patch("Community-Event am Wochenende")


def test_truncate():
    text, cut = truncate("kurz", 100)
    assert (text, cut) == ("kurz", False)
    long = "Wort " * 100
    text, cut = truncate(long, 50)
    assert cut and len(text) <= 50 and text.endswith("…")
