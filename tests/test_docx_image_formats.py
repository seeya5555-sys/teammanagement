"""입거/승선 보고서 DOCX 에 들어가는 사진의 포맷 계약.

왜 필요한가
----------
사진은 전부 `dock_report_docx._crop_to_aspect` 를 거쳐 `run.add_picture` 로 들어간다.
그 함수는 "비율이 이미 맞으면 원본 경로를 그대로 돌려준다" 는 최적화를 갖고 있었는데,
2026-08-22 에 HEIC 디코드를 켜면서 그게 구멍이 됐다.

  · 등록 전: HEIC 는 `Image.open` 에서 예외 → except → 원본 경로. Word 는 못 그림.
  · 등록 후: 4:3 HEIC 는 "비율이 맞다" 로 판정되어 **역시 원본 경로**. Word 는 여전히 못 그림.

두 번째가 더 나쁘다. 삽입 자체는 성공해서 로그에도 안 남고, 형이 Word 를 열어봐야
빈 칸인 걸 안다. 그래서 Word 가 못 읽는 포맷은 자를 게 없어도 JPEG 로 굽는다.
"""
import os
import unittest

import dock_report_docx


def _heic_bytes(path, size):
    try:
        import pillow_heif
    except Exception:
        return False
    from PIL import Image
    pillow_heif.register_heif_opener()
    Image.new('RGB', size, (120, 40, 200)).save(path, format='HEIF')
    return True


class SafeFormatContractTests(unittest.TestCase):
    def test_allowlist_is_by_real_format_not_by_file_name(self):
        """판정 근거가 확장자로 되돌아가면 이름만 .jpg 인 HEIF 가 그대로 Word 로 간다."""
        self.assertIn('JPEG', dock_report_docx._DOCX_SAFE_FORMATS)
        self.assertNotIn('HEIF', dock_report_docx._DOCX_SAFE_FORMATS)
        self.assertFalse(hasattr(dock_report_docx, '_DOCX_UNSUPPORTED_EXT'),
                         '확장자 blocklist 로 되돌리지 말 것')


class CropToAspectFormatTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._dir = tempfile.mkdtemp(prefix='trmt_docx_img_')
        self._made = []

    def tearDown(self):
        import shutil
        for p in self._made:
            if os.path.exists(p):
                os.unlink(p)
        shutil.rmtree(self._dir, ignore_errors=True)

    def _out(self, path):
        if path not in (None,) and path.startswith('/') and 'dock_img_' in path:
            self._made.append(path)
        return path

    def test_jpeg_already_at_ratio_is_passed_through(self):
        """기존 최적화는 그대로 — 4:3 JPEG 는 재인코딩 없이 원본 경로를 쓴다."""
        from PIL import Image
        src = os.path.join(self._dir, 'ok.jpg')
        Image.new('RGB', (1200, 900), (10, 10, 10)).save(src, format='JPEG')
        self.assertEqual(src, self._out(dock_report_docx._crop_to_aspect(src, 4 / 3)))

    def test_heic_at_ratio_is_reencoded_even_though_nothing_needs_cropping(self):
        """🔴 비율이 맞아도 Word 가 못 읽는 포맷이면 변환한다. 안 하면 빈 칸이 나간다."""
        src = os.path.join(self._dir, 'iphone.heic')
        if not _heic_bytes(src, (1200, 900)):
            self.skipTest('pillow-heif 미설치 — HEIC 경로를 검증할 수 없다')
        out = self._out(dock_report_docx._crop_to_aspect(src, 4 / 3))
        self.assertNotEqual(src, out, 'HEIC 원본 경로를 그대로 넘기면 Word 에서 빈 칸이다')
        self.assertTrue(out.endswith('.jpg'))
        from PIL import Image
        with Image.open(out) as im:
            self.assertEqual('JPEG', im.format)
            self.assertEqual((1200, 900), im.size, '자를 게 없으므로 크기는 그대로여야 한다')

    def test_heif_bytes_wearing_a_jpg_name_are_still_reencoded(self):
        """🔴 확장자 위장. 공유앱을 거친 아이폰 사진에서 실제로 나온다."""
        src = os.path.join(self._dir, 'looks_like.jpg')
        if not _heic_bytes(src, (1200, 900)):
            self.skipTest('pillow-heif 미설치 — HEIC 경로를 검증할 수 없다')
        out = self._out(dock_report_docx._crop_to_aspect(src, 4 / 3))
        self.assertNotEqual(src, out, '이름이 .jpg 여도 바이트가 HEIF 면 Word 는 못 그린다')
        from PIL import Image
        with Image.open(out) as im:
            self.assertEqual('JPEG', im.format)

    def test_heic_off_ratio_is_cropped_like_any_other_photo(self):
        """포맷 때문에 크롭 계약이 달라지면 안 된다 — 세로 HEIC 도 4:3 으로 잘린다."""
        src = os.path.join(self._dir, 'tall.heic')
        if not _heic_bytes(src, (900, 1600)):
            self.skipTest('pillow-heif 미설치 — HEIC 경로를 검증할 수 없다')
        out = self._out(dock_report_docx._crop_to_aspect(src, 4 / 3))
        from PIL import Image
        with Image.open(out) as im:
            self.assertEqual('JPEG', im.format)
            self.assertAlmostEqual(4 / 3, im.size[0] / im.size[1], places=2)

    def test_unreadable_bytes_still_fall_back_to_the_original_path(self):
        """디코드 실패는 예전처럼 조용히 원본을 쓴다(여기서 예외가 나가면 보고서가 통째로 죽는다)."""
        src = os.path.join(self._dir, 'broken.heic')
        with open(src, 'wb') as f:
            f.write(b'\x00\x00\x00\x18ftypheic' + b'0' * 32)
        self.assertEqual(src, self._out(dock_report_docx._crop_to_aspect(src, 4 / 3)))


if __name__ == '__main__':
    unittest.main()
