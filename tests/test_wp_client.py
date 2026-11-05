"""
Testes de publish_to_wordpress sem rede (requests é substituído por um WP falso).

    python -m unittest discover -s tests -v

Caso coberto: recoletar um arquivo que já está anexado ao dataset não pode enviar
um novo anexo — o WordPress gravaria `al-cvli-202610-1.csv`, `-2.csv`… e deixaria o
anexo anterior órfão (a linha do repeater era trocada, mas a mídia ficava).
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import wp_client  # noqa: E402

WP = "http://wp.test"
POST_ID = 351


class FakeResponse:
    def __init__(self, status=200, payload=None, content=b""):
        self.status_code = status
        self._payload = payload
        self._content = content
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        return self._payload

    def iter_content(self, size):
        for i in range(0, len(self._content), size):
            yield self._content[i : i + size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeWP:
    """WP mínimo: um post `dataset` com repeater `arquivos` e anexos em memória."""

    def __init__(self, attachments: dict, rows: list):
        # attachments: {id: (nome do arquivo, bytes)}
        self.attachments = dict(attachments)
        self.rows = list(rows)
        self.next_id = 900
        self.uploads = []
        self.saved_payloads = []

    def url_of(self, mid):
        return f"{WP}/wp-content/uploads/datasets/AL/al_cvli/{self.attachments[mid][0]}"

    def get(self, url, params=None, **kw):
        params = params or {}
        if url == f"{WP}/wp-json/wp/v2/dataset/{POST_ID}":
            return FakeResponse(payload={"id": POST_ID, "status": "publish",
                                         "acf": {"arquivos": self.rows}})
        if url == f"{WP}/wp-json/wp/v2/media":
            ids = [int(i) for i in params["include"].split(",")]
            return FakeResponse(payload=[
                {"id": i, "source_url": self.url_of(i),
                 "media_details": {"filesize": len(self.attachments[i][1])}}
                for i in ids if i in self.attachments
            ])
        for mid, (name, content) in self.attachments.items():
            if url == self.url_of(mid):
                return FakeResponse(content=content)
        if "/wp/v2/instituicao" in url:
            return FakeResponse(payload=[])
        return FakeResponse(status=404, payload={})

    def post(self, url, data=None, files=None, **kw):
        if url.endswith("/observadados/v1/upload-dataset"):
            name, fh, _ = files["file"]
            content = fh.read()
            existing = {n for n, _ in self.attachments.values()}
            stem, ext = os.path.splitext(name)
            final, n = name, 0
            while final in existing:  # wp_unique_filename
                n += 1
                final = f"{stem}-{n}{ext}"
            mid = self.next_id
            self.next_id += 1
            self.attachments[mid] = (final, content)
            self.uploads.append(final)
            return FakeResponse(payload={"success": True, "id": mid, "url": self.url_of(mid)})
        if url == f"{WP}/wp-json/wp/v2/dataset/{POST_ID}":
            payload = json.loads(data)
            self.saved_payloads.append(payload)
            self.rows = payload["acf"]["arquivos"]
            return FakeResponse(payload={"id": POST_ID})
        return FakeResponse(status=404, payload={})


def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE sources (id INTEGER PRIMARY KEY, wp_title, wp_description, acf_subtitulo,
           acf_origem, acf_frequencia, acf_formato, wp_post_id, collector_key, uf, url,
           wp_content, acf_tratamento)"""
    )
    conn.execute(
        "INSERT INTO sources (id, wp_title, wp_post_id, collector_key, uf, url) VALUES (1, 'CVLI AL', ?, 'al_cvli', 'AL', 'http://src')",
        (POST_ID,),
    )
    return conn


def row(mid, data="01/10/2026"):
    return {"descricao": "", "data": data, "arquivo": mid, "formato": 5, "metadados": "{}"}


class PublishDedupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.csv = Path(self.tmp.name) / "al-cvli-202610.csv"
        self.content = b"data;municipio;vitimas\n2026-10-01;Maceio;3\n"
        self.csv.write_bytes(self.content)
        patches = [
            mock.patch.object(wp_client, "_load_env", lambda *a, **k: None),
            mock.patch.object(wp_client, "resolve_dataset_post", lambda *a, **k: POST_ID),
            mock.patch.object(wp_client, "forget_cache", lambda: None),
            mock.patch.dict(os.environ, {"WP_URL": WP, "WP_USER": "u", "WP_APP_PASSWORD": "p"}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def publish(self, wp):
        with mock.patch.object(wp_client.requests, "get", wp.get), \
             mock.patch.object(wp_client.requests, "post", wp.post):
            return wp_client.publish_to_wordpress(make_conn(), 1, self.csv, '{"v": 2}')

    def test_recoleta_do_mesmo_arquivo_nao_reenvia(self):
        wp = FakeWP({350: ("al-cvli-202605.csv", b"maio"), 497: ("al-cvli-202610.csv", self.content)},
                    [row(350, "28/05/2026"), row(497)])
        for _ in range(3):  # três coletas seguidas, como no AL
            media_id, post_id, _ = self.publish(wp)
            self.assertEqual((media_id, post_id), (497, POST_ID))
        self.assertEqual(wp.uploads, [])
        self.assertEqual([r["arquivo"] for r in wp.rows], [350, 497])
        self.assertEqual(wp.rows[-1]["metadados"], '{"v": 2}')

    def test_anexo_com_sufixo_wp_e_mesmo_conteudo_e_reaproveitado(self):
        wp = FakeWP({456: ("al-cvli-202610-1.csv", self.content)}, [row(456)])
        media_id, _, _ = self.publish(wp)
        self.assertEqual(media_id, 456)
        self.assertEqual(wp.uploads, [])
        self.assertEqual([r["arquivo"] for r in wp.rows], [456])

    def test_conteudo_diferente_envia_e_substitui_a_linha(self):
        wp = FakeWP({497: ("al-cvli-202610.csv", b"versao antiga")}, [row(497)])
        media_id, _, _ = self.publish(wp)
        self.assertNotEqual(media_id, 497)
        self.assertEqual(len(wp.uploads), 1)
        self.assertEqual([r["arquivo"] for r in wp.rows], [media_id])

    def test_periodo_novo_envia_e_mantem_os_anteriores(self):
        wp = FakeWP({350: ("al-cvli-202605.csv", b"maio")}, [row(350)])
        media_id, _, _ = self.publish(wp)
        self.assertEqual(wp.uploads, ["al-cvli-202610.csv"])
        self.assertEqual([r["arquivo"] for r in wp.rows], [350, media_id])

    def test_upload_so_acontece_depois_de_ler_o_post(self):
        wp = FakeWP({}, [])
        calls = []
        get, post = wp.get, wp.post
        wp.get = lambda url, **k: (calls.append(("get", url)), get(url, **k))[1]
        wp.post = lambda url, **k: (calls.append(("post", url)), post(url, **k))[1]
        self.publish(wp)
        first_upload = next(i for i, c in enumerate(calls) if c[1].endswith("upload-dataset"))
        self.assertLess(calls.index(("get", f"{WP}/wp-json/wp/v2/dataset/{POST_ID}")), first_upload)


class SameFileTest(unittest.TestCase):
    def test_sufixo_wp(self):
        self.assertTrue(wp_client._same_file("al-cvli-202610-2.csv", "al-cvli-202610.csv"))
        self.assertTrue(wp_client._same_file("ba-cvli-cvli-bahia-2026-202611-1.csv",
                                             "ba-cvli-cvli-bahia-2026-202611.csv"))

    def test_periodo_diferente(self):
        self.assertFalse(wp_client._same_file("al-cvli-202605.csv", "al-cvli-202610.csv"))
        self.assertFalse(wp_client._same_file(None, "al-cvli-202610.csv"))


if __name__ == "__main__":
    unittest.main()
