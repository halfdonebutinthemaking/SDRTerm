"""Tests for the POCSAG BCH(31,21) codec."""
import pytest

from plugins.pocsag.bch import encode, correct, decode_codeword


class TestBCHEncoding:
    def test_encoded_syndrome_is_zero(self):
        for data21 in [0, 0x1FFFFF, 0x155555, 0x0AAAAA, 0x123456]:
            cw = encode(data21)
            _, n = correct(cw)
            assert n == 0

    def test_encoded_preserves_data_bits(self):
        for data21 in [0x123456, 0x0ABCDE, 0x1F00F0, 0x000001]:
            cw = encode(data21)
            recovered = (cw >> 10) & 0x1FFFFF
            assert recovered == data21


class TestBCHCorrection:
    _DATA = 0x155555

    def test_no_error(self):
        cw = encode(self._DATA)
        result, n = correct(cw)
        assert result == cw
        assert n == 0

    @pytest.mark.parametrize('bit', list(range(31)))
    def test_single_bit_error(self, bit):
        cw        = encode(self._DATA)
        corrupted = cw ^ (1 << bit)
        result, n = correct(corrupted)
        assert n == 1
        assert result == cw

    def test_two_bit_errors(self):
        cw = encode(self._DATA)
        for i in range(0, 31, 3):
            for j in range(i + 1, 31, 5):
                corrupted = cw ^ (1 << i) ^ (1 << j)
                result, n = correct(corrupted)
                assert 1 <= n <= 2
                assert result == cw


class TestFullCodewordDecoding:
    def test_correct_message_codeword_with_parity(self):
        # Build a valid 32-bit POCSAG codeword by hand
        data21 = (1 << 20) | 0x12345          # message indicator + 20 data bits
        cw31   = encode(data21)
        parity = bin(cw31).count('1') & 1
        word32 = (cw31 << 1) | parity

        recovered, n, ok = decode_codeword(word32)
        assert recovered == word32
        assert n == 0
        assert ok is True

    def test_correct_address_codeword_with_parity(self):
        data21 = (0 << 20) | (0x1AB << 2) | 3    # address indicator + addr + func
        cw31   = encode(data21)
        parity = bin(cw31).count('1') & 1
        word32 = (cw31 << 1) | parity

        recovered, n, ok = decode_codeword(word32)
        assert n == 0
        assert ok is True
        assert ((recovered >> 31) & 1) == 0        # address indicator preserved
        assert ((recovered >> 11) & 0x3) == 3      # function code preserved
