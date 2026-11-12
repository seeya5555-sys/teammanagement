import unittest
from unittest import mock
import helpers_shared as H


class TestHangulRetry(unittest.TestCase):
    def test_retry_single_when_hangul_left(self):
        calls = []
        def batch(texts, group):
            calls.append(list(group))
            if len(group) > 1:
                return {0: "Continuous VDR alarm 발생 (Alarm Code 6011)", 1: "Main engine check"}
            return {0: "Continuous VDR alarm issue (Alarm Code 6011)"}
        with mock.patch.object(H, "GEMINI_API_KEY", "x"), mock.patch.object(H, "_translate_batch_en", side_effect=batch):
            out = H._translate_texts_en(["VDR 연속 Alarm 발생 (Alarm Code 6011)", "주기관 점검"])
        self.assertEqual(out, ["Continuous VDR alarm issue (Alarm Code 6011)", "Main engine check"])
        self.assertEqual(calls[-1], [0])

    def test_keep_first_result_if_retry_still_hangul(self):
        with mock.patch.object(H, "GEMINI_API_KEY", "x"), \
             mock.patch.object(H, "_translate_batch_en", side_effect=lambda t, g: {i: "VDR 발생" for i in g}):
            self.assertEqual(H._translate_texts_en(["VDR 발생"]), ["VDR 발생"])

    def test_empty_retry_result_not_adopted(self):
        def batch(texts, group):
            return {0: "VDR alarm 발생"} if len(calls) == 0 or calls.append(1) else {0: "  "}
        calls = []
        seq = iter([{0: "VDR alarm 발생"}, {0: "  "}])
        with mock.patch.object(H, "GEMINI_API_KEY", "x"), mock.patch.object(H, "_translate_batch_en", side_effect=lambda t, g: next(seq)):
            self.assertEqual(H._translate_texts_en(["VDR 알람 발생"]), ["VDR alarm 발생"])

    def test_no_retry_when_clean(self):
        calls = []
        def batch(texts, group):
            calls.append(group); return {i: "ok" for i in group}
        with mock.patch.object(H, "GEMINI_API_KEY", "x"), mock.patch.object(H, "_translate_batch_en", side_effect=batch):
            H._translate_texts_en(["가", "나"])
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
