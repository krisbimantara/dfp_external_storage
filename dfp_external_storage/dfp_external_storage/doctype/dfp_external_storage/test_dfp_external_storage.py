# Copyright (c) 2023, DFP and Contributors
# See license.txt

import io
from unittest.mock import Mock, patch

import frappe
from frappe.core.doctype.file.file import File
from frappe.tests import UnitTestCase

from . import dfp_external_storage as storage_module
from .dfp_external_storage import DFPExternalStorage, DFPExternalStorageFile, S3FileProxy


def make_file(**fields):
	# Construct only the in-memory fields needed by these tests. No site, DB or
	# bucket connection is needed, and core lifecycle calls are mocked explicitly.
	doc = object.__new__(DFPExternalStorageFile)
	doc.__dict__.update({
		"doctype": "File", "name": "test-file", "file_name": "file.txt",
		"file_url": "/file/source/file.txt", "folder": "Home", "content": b"",
		"content_hash": "known-hash", "dfp_external_storage": "storage",
		"dfp_external_storage_s3_key": "key", "flags": frappe._dict(), **fields,
	})
	return doc


def make_storage(folders):
	doc = object.__new__(DFPExternalStorage)
	doc.__dict__.update({
		"name": "storage", "folders": [frappe._dict(folder=folder) for folder in folders],
	})
	return doc


class TestFileContent(UnitTestCase):
	def remote_file(self, content):
		doc = make_file()
		doc.dfp_is_s3_remote_file = Mock(return_value=True)
		doc.is_downloadable = Mock(return_value=True)
		doc.dfp_external_storage_download_file = Mock(return_value=content)
		return doc

	def test_local_file_forwards_default_and_explicit_encodings(self):
		doc = make_file(dfp_external_storage_s3_key="", file_url="/files/file.txt")
		doc.dfp_is_s3_remote_file = Mock(return_value=False)
		for encodings in (None, [], ["utf-8-sig", "utf-8"]):
			with self.subTest(encodings=encodings), patch.object(File, "get_content", return_value=b"local") as core:
				self.assertEqual(doc.get_content(encodings=encodings), b"local")
				core.assert_called_once_with(encodings=encodings)

	def test_remote_default_decodes_utf8_and_strips_bom(self):
		doc = self.remote_file("nama;jumlah\nJuragan;5\n".encode("utf-8-sig"))
		self.assertEqual(doc.get_content(), "nama;jumlah\nJuragan;5\n")
		self.assertEqual(doc._content, "nama;jumlah\nJuragan;5\n")

	def test_remote_explicit_alternative_encoding(self):
		doc = self.remote_file("Caf\u00e9".encode("utf-16"))
		self.assertEqual(doc.get_content(encodings=["utf-8", "utf-16"]), "Caf\u00e9")

	def test_remote_empty_encoding_list_preserves_bytes(self):
		raw = "nama;jumlah\nJuragan;5\n".encode("utf-8-sig")
		self.assertEqual(self.remote_file(raw).get_content(encodings=[]), raw)

	def test_remote_zip_and_ole_remain_binary_even_with_text_codec(self):
		for raw in (b"PK\x03\x04xlsx", b"PK\x05\x06empty-zip", b"PK\x07\x08zip",
			b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1xls"):
			with self.subTest(signature=raw[:8]):
				self.assertEqual(self.remote_file(raw).get_content(), raw)
				self.assertEqual(self.remote_file(raw).get_content(encodings=["latin-1"]), raw)

	def test_remote_access_denial_prevents_download(self):
		doc = self.remote_file(b"private")
		doc.is_downloadable.return_value = False
		with self.assertRaises(frappe.PageDoesNotExistError):
			doc.get_content()
		doc.dfp_external_storage_download_file.assert_not_called()


class TestManagedFileLifecycle(UnitTestCase):
	def test_known_managed_hash_survives_core_remote_hash_clear(self):
		for method in ("before_insert", "validate"):
			with self.subTest(method=method):
				doc = make_file()
				with patch.object(File, method, autospec=True, side_effect=lambda document: setattr(document, "content_hash", None)) as core:
					getattr(doc, method)()
					core.assert_called_once_with(doc)
				self.assertEqual(doc.content_hash, "known-hash")

	def test_unknown_managed_hash_is_not_fabricated(self):
		doc = make_file(content_hash=None)
		with patch.object(File, "validate", return_value=None):
			doc.validate()
		self.assertIsNone(doc.content_hash)

	def test_generic_remote_url_keeps_core_hash_clear_even_with_stale_storage_metadata(self):
		for key in ("", "stale-key"):
			for method in ("before_insert", "validate"):
				with self.subTest(key=key, method=method):
					doc = make_file(dfp_external_storage_s3_key=key, file_url="https://example.test/file.txt")
					with patch.object(File, method, autospec=True, side_effect=lambda document: setattr(document, "content_hash", None)):
						getattr(doc, method)()
					self.assertIsNone(doc.content_hash)

	def test_core_private_copy_check_still_rejects_insert(self):
		doc = make_file()
		doc.flags.copy_from_existing_file = True
		with patch.object(File, "before_insert", side_effect=frappe.PermissionError) as core:
			with self.assertRaises(frappe.PermissionError):
				doc.before_insert()
		core.assert_called_once_with()
		self.assertTrue(doc.flags.copy_from_existing_file)

	def test_copy_restores_s3_metadata_before_core_validation(self):
		doc = make_file(dfp_external_storage="", dfp_external_storage_s3_key="", content_hash=None)
		source = frappe._dict(dfp_external_storage="source-storage", dfp_external_storage_s3_key="source-key",
			content_hash="source-hash", file_size=25)
		with patch.object(frappe, "get_value", return_value=source) as lookup:
			with patch.object(File, "before_insert", autospec=True, side_effect=lambda document: setattr(document, "content_hash", None)):
				doc.before_insert()
		lookup.assert_called_once_with("File", "source", fieldname="*")
		self.assertEqual(doc.dfp_external_storage, "source-storage")
		self.assertEqual(doc.dfp_external_storage_s3_key, "source-key")
		self.assertEqual(doc.content_hash, "source-hash")
		self.assertTrue(doc.flags.ignore_duplicate_entry_error)


class TestStorageFolders(UnitTestCase):
	def setUp(self):
		super().setUp()
		translation = patch.object(storage_module, "_", side_effect=lambda message: message)
		translation.start()
		self.addCleanup(translation.stop)

	def test_empty_folder_assignment_does_not_query_database(self):
		with patch.object(frappe, "get_all") as query:
			make_storage([]).validate_folder_assignments()
		query.assert_not_called()

	def test_duplicate_folder_in_one_storage_is_rejected(self):
		with patch.object(frappe, "throw", side_effect=frappe.ValidationError), patch.object(frappe, "get_all") as query:
			with self.assertRaises(frappe.ValidationError):
				make_storage(["Home", "Home"]).validate_folder_assignments()
		query.assert_not_called()

	def test_folder_assigned_to_other_storage_is_rejected(self):
		conflict = frappe._dict(folder="Home", parent="other-storage")
		with patch.object(frappe, "get_all", return_value=[conflict]) as query:
			with patch.object(frappe, "throw", side_effect=frappe.ValidationError):
				with self.assertRaises(frappe.ValidationError):
					make_storage(["Home"]).validate_folder_assignments()
		self.assertEqual(query.call_args.kwargs["filters"]["parent"], ["!=", "storage"])

	def test_distinct_folder_assignment_is_allowed(self):
		with patch.object(frappe, "get_all", return_value=[]):
			make_storage(["Home", "Home/Images"]).validate_folder_assignments()

	def test_folder_and_home_fallback_have_explicit_stable_order(self):
		doc = make_file(dfp_external_storage="", dfp_external_storage_s3_key="", folder="Home/Images")
		with patch.object(frappe, "db", Mock()) as database, patch.object(frappe, "get_doc", return_value="resolved"):
			database.get_value.side_effect = [None, "home-storage"]
			self.assertEqual(doc.dfp_external_storage_doc, "resolved")
		self.assertEqual(len(database.get_value.call_args_list), 2)
		for call in database.get_value.call_args_list:
			self.assertEqual(call.kwargs["order_by"], "modified desc, name desc")
			self.assertEqual(call.kwargs["filters"]["parenttype"], "DFP External Storage")


class TestS3FileProxy(UnitTestCase):
	def proxy(self, raw=b"abcdef"):
		read = Mock(side_effect=lambda offset, length: raw[offset:offset + length])
		return S3FileProxy(read, len(raw)), read

	def test_zero_read_and_empty_file_make_no_range_request(self):
		proxy, read = self.proxy()
		self.assertEqual(proxy.read(0), b"")
		read.assert_not_called()
		empty, read = self.proxy(b"")
		self.assertEqual(empty.read(), b"")
		read.assert_not_called()

	def test_stream_reads_until_eof_without_out_of_range_request(self):
		proxy, read = self.proxy()
		self.assertEqual(proxy.read(4), b"abcd")
		self.assertEqual(proxy.read(4), b"ef")
		self.assertEqual(proxy.read(4), b"")
		self.assertEqual([call.args for call in read.call_args_list], [(0, 4), (4, 2)])

	def test_default_read_gets_remaining_content_after_seek(self):
		proxy, read = self.proxy()
		self.assertEqual(proxy.seek(-2, io.SEEK_END), 4)
		self.assertEqual(proxy.read(), b"ef")
		read.assert_called_once_with(4, 2)

	def test_seek_before_start_and_invalid_mode_are_rejected(self):
		proxy, _ = self.proxy()
		for offset, whence in ((-1, io.SEEK_SET), (-7, io.SEEK_END), (0, 99)):
			with self.subTest(offset=offset, whence=whence), self.assertRaises(ValueError):
				proxy.seek(offset, whence)
		self.assertEqual(proxy.tell(), 0)
