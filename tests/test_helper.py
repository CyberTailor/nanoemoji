# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import difflib
import io
import os
import re
import shutil
import subprocess
import sys
from lxml import etree
from fontTools import ttLib
from nanoemoji import codepoints
from nanoemoji import config
from nanoemoji import features
from nanoemoji.glyph import glyph_name
from nanoemoji import write_font
from pathlib import Path
from picosvg.svg import SVG
import pytest
import tempfile


def test_data_dir() -> Path:
    return Path(__file__).parent


def locate_test_file(filename) -> Path:
    return test_data_dir() / filename


def parse_svg(filename, locate=False, topicosvg=True):
    if locate:
        filename = locate_test_file(filename)
    svg = SVG.parse(filename)
    return svg.topicosvg(inplace=True) if topicosvg else svg


def rasterize_svg(filename, bitmap_resolution):
    resvg = shutil.which("resvg")
    if not resvg:
        pytest.skip("resvg not installed")
    with tempfile.TemporaryDirectory(prefix="nanoemoji-resvg-") as tmpdir:
        output_file = Path(tmpdir) / Path(filename).with_suffix(".png").name
        result = subprocess.run(
            [
                resvg,
                "-h",
                f"{bitmap_resolution}",
                "-w",
                f"{bitmap_resolution}",
                filename,
                output_file,
            ]
        )
        return output_file.read_bytes()


def color_font_config(config_overrides, svgs, tmp_dir=None):
    if tmp_dir is None:
        tmp_dir = tempfile.gettempdir()
    svgs = tuple(locate_test_file(s) for s in svgs)
    fea_file = os.path.join(tmp_dir, "test.fea")
    rgi_seqs = tuple(codepoints.from_filename(str(f)) for f in svgs)
    with open(fea_file, "w") as f:
        f.write(features.generate_fea(rgi_seqs))

    font_config = (
        config.load(config_file=None, additional_srcs=svgs)
        ._replace(
            family="UnitTest",
            upem=100,
            ascender=100,
            descender=0,
            width=100,
            keep_glyph_names=True,
            fea_file=fea_file,
        )
        ._replace(**config_overrides)
    )

    topicosvg = True if font_config.has_picosvgs else False

    return (
        font_config,
        [
            write_font.InputGlyph(
                (os.path.relpath(svg),),
                (0xE000 + idx,),
                glyph_name((0xE000 + idx,)),
                parse_svg(svg, topicosvg=topicosvg),
                bitmap=(
                    rasterize_svg(svg, font_config.bitmap_resolution)
                    if font_config.has_bitmaps
                    else None
                ),
            )
            for idx, svg in enumerate(svgs)
        ],
    )


def reload_font(ttfont):
    tmp = io.BytesIO()
    ttfont.save(tmp)
    return ttLib.TTFont(tmp)


def _save_actual_ttx(expected_ttx, ttx_content):
    tmp_file = os.path.join(tempfile.gettempdir(), expected_ttx)
    with open(tmp_file, "w") as f:
        f.write(ttx_content)
    return tmp_file


def _strip_inline_bitmaps(ttx_content):
    parser = etree.XMLParser(strip_cdata=False)
    root = etree.fromstring(bytes(ttx_content, encoding="utf-8"), parser=parser)
    made_changes = False

    # bitmapGlyphDataFormat="extfile" doesn't work for sbix so wipe those manually
    for hexdata in root.xpath("//sbix/strike/glyph/hexdata"):
        glyph = hexdata.getparent()
        glyph.remove(hexdata)
        glyph.text = (glyph.attrib["name"] + "." + glyph.attrib["graphicType"]).strip()
        made_changes = True

    # Windows gives \ instead of /, if we see that flip it
    for imagedata in root.xpath("//extfileimagedata"):
        imagedata.attrib["value"] = Path(imagedata.attrib["value"]).name
        made_changes = True

    if not made_changes:
        return ttx_content

    actual_ttx = io.BytesIO()
    etree.ElementTree(root).write(actual_ttx, encoding="utf-8")
    # Glue on the *exact* xml decl and wrapping newline saveXML produces
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        + actual_ttx.getvalue().decode("utf-8")
        + "\n"
    )


def assert_expected_ttx(
    svgs,
    ttfont,
    expected_ttx,
    include_tables=None,
    skip_tables=("head", "hhea", "maxp", "name", "post", "OS/2"),
):
    actual_ttx = io.StringIO()
    # Timestamps inside files #@$@#%@#
    # force consistent Unix newlines (the expected test files use \n too)
    ttfont.saveXML(
        actual_ttx,
        newlinestr="\n",
        tables=include_tables,
        skipTables=skip_tables,
        bitmapGlyphDataFormat="extfile",
    )

    # Elide ttFont attributes because ttLibVersion may change
    actual = re.sub(r'\s+ttLibVersion="[^"]+"', "", actual_ttx.getvalue())

    actual = _strip_inline_bitmaps(actual)

    expected_location = locate_test_file(expected_ttx)
    if os.path.isfile(expected_location):
        with open(expected_location) as f:
            expected = f.read()
    else:
        tmp_file = _save_actual_ttx(expected_ttx, actual)
        raise FileNotFoundError(
            f"Missing expected in {expected_location}. Actual in {tmp_file}"
        )

    if actual != expected:
        for line in difflib.unified_diff(
            expected.splitlines(keepends=True),
            actual.splitlines(keepends=True),
            fromfile=f"{expected_ttx} (expected)",
            tofile=f"{expected_ttx} (actual)",
        ):
            sys.stderr.write(line)
        print(f"SVGS: {svgs}")
        tmp_file = _save_actual_ttx(expected_ttx, actual)
        pytest.fail(f"{tmp_file} != {expected_ttx}")


# Copied from picosvg
def drop_whitespace(svg):
    svg._update_etree()
    for el in svg.svg_root.iter("*"):
        if el.text is not None:
            el.text = el.text.strip()
        if el.tail is not None:
            el.tail = el.tail.strip()


# Copied from picosvg
def pretty_print(svg_tree):
    def _reduce_text(text):
        text = text.strip() if text else None
        return text if text else None

    # lxml really likes to retain whitespace
    for e in svg_tree.iter("*"):
        e.text = _reduce_text(e.text)
        e.tail = _reduce_text(e.tail)

    return etree.tostring(svg_tree, pretty_print=True).decode("utf-8")


# Copied from picosvg
def svg_diff(actual_svg: SVG, expected_svg: SVG):
    drop_whitespace(actual_svg)
    drop_whitespace(expected_svg)
    print(f"A: {pretty_print(actual_svg.toetree())}")
    print(f"E: {pretty_print(expected_svg.toetree())}")
    assert actual_svg.tostring() == expected_svg.tostring()
