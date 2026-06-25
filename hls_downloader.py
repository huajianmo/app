#!/usr/bin/env python3
"""
HLS/M3U8 多线程下载、合并与格式转换工具

用法:
    python hls_downloader.py <m3u8_url> -o <输出文件>

示例:
    python hls_downloader.py https://example.com/playlist.m3u8 -o video.mp4
    python hls_downloader.py https://example.com/playlist.m3u8 -o audio.m4a --audio-only
    python hls_downloader.py https://example.com/playlist.m3u8 -o video.mp4 --threads 8 --mode instant
    python hls_downloader.py https://example.com/playlist.m3u8 -o audio.mp3 --audio-only
"""

import os
import re
import sys
import time
import shutil
import argparse
import subprocess
import threading
from pathlib import Path
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import requests


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class KeyInfo:
    method: str
    uri: Optional[str]
    iv: Optional[bytes]


@dataclass
class Segment:
    index: int
    url: str
    duration: float
    key: Optional[KeyInfo] = field(default=None)


# ---------------------------------------------------------------------------
# Core downloader
# ---------------------------------------------------------------------------

class HLSDownloader:
    def __init__(
        self,
        url: str,
        output: str,
        threads: int = 4,
        mode: str = "batch",
        audio_only: bool = False,
        temp_dir: Optional[str] = None,
        retries: int = 3,
        extra_headers: Optional[Dict[str, str]] = None,
    ):
        self.url = url
        self.output = Path(output)
        self.threads = threads
        self.mode = mode          # "batch" | "instant"
        self.audio_only = audio_only
        self.retries = retries

        self.output_format = self.output.suffix.lower().lstrip(".") or "mp4"

        self.temp_dir = Path(temp_dir) if temp_dir else Path(f"_hls_tmp_{self.output.stem}")

        self.headers: Dict[str, str] = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
        if extra_headers:
            self.headers.update(extra_headers)

        self.segments: List[Segment] = []
        self._key_cache: Dict[str, bytes] = {}
        self._lock = threading.Lock()
        self._completed = 0

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self):
        print(f"[HLS] 解析播放列表: {self.url}")
        self.segments = self._parse_m3u8(self.url)
        total = len(self.segments)
        print(f"[HLS] 共发现 {total} 个片段，使用 {self.threads} 线程下载")

        self.temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            if self.mode == "instant":
                self._run_instant()
            else:
                self._run_batch()
        finally:
            shutil.rmtree(self.temp_dir, ignore_errors=True)

        print(f"\n[HLS] 完成！输出文件: {self.output}")

    # ------------------------------------------------------------------
    # M3U8 parsing
    # ------------------------------------------------------------------

    def _parse_m3u8(self, url: str) -> List[Segment]:
        content = self._get(url).text
        base_url = url.rsplit("/", 1)[0] + "/"

        if "#EXT-X-STREAM-INF" in content:
            print("[HLS] 检测到主播放列表，自动选择最高码率流...")
            url = self._select_best_stream(content, base_url)
            print(f"[HLS] 已选流: {url}")
            content = self._get(url).text
            base_url = url.rsplit("/", 1)[0] + "/"

        return self._parse_media_playlist(content, base_url)

    def _select_best_stream(self, content: str, base_url: str) -> str:
        streams: List[tuple] = []
        lines = content.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF"):
                bw_match = re.search(r"BANDWIDTH=(\d+)", line)
                bandwidth = int(bw_match.group(1)) if bw_match else 0
                if i + 1 < len(lines):
                    uri = lines[i + 1].strip()
                    if uri and not uri.startswith("#"):
                        full_url = uri if uri.startswith("http") else urljoin(base_url, uri)
                        streams.append((bandwidth, full_url))

        if not streams:
            raise ValueError("主播放列表中未找到可用流")

        streams.sort(key=lambda x: x[0], reverse=True)
        print("[HLS] 可用流（按带宽降序）:")
        for bw, u in streams:
            print(f"       {bw // 1000:>6} kbps  {u}")

        return streams[0][1]

    def _parse_media_playlist(self, content: str, base_url: str) -> List[Segment]:
        segments: List[Segment] = []
        lines = content.splitlines()
        current_duration = 0.0
        current_key: Optional[KeyInfo] = None
        index = 0

        for i, line in enumerate(lines):
            line = line.strip()

            if line.startswith("#EXT-X-KEY"):
                current_key = self._parse_key_tag(line, base_url)

            elif line.startswith("#EXTINF"):
                m = re.search(r"#EXTINF:([\d.]+)", line)
                if m:
                    current_duration = float(m.group(1))

            elif line and not line.startswith("#"):
                url = line if line.startswith("http") else urljoin(base_url, line)
                segments.append(Segment(
                    index=index,
                    url=url,
                    duration=current_duration,
                    key=current_key,
                ))
                index += 1
                current_duration = 0.0

        return segments

    def _parse_key_tag(self, line: str, base_url: str) -> Optional[KeyInfo]:
        method_m = re.search(r'METHOD=([^,\s]+)', line)
        uri_m = re.search(r'URI="([^"]+)"', line)
        iv_m = re.search(r'IV=0x([0-9a-fA-F]+)', line)

        if not method_m:
            return None
        method = method_m.group(1)
        if method == "NONE":
            return None

        uri = uri_m.group(1) if uri_m else None
        if uri and not uri.startswith("http"):
            uri = urljoin(base_url, uri)

        iv = bytes.fromhex(iv_m.group(1)) if iv_m else None
        return KeyInfo(method=method, uri=uri, iv=iv)

    # ------------------------------------------------------------------
    # Segment downloading
    # ------------------------------------------------------------------

    def _download_segment(self, seg: Segment) -> Path:
        ts_path = self.temp_dir / f"seg_{seg.index:06d}.ts"

        if ts_path.exists() and ts_path.stat().st_size > 0:
            return ts_path

        last_err: Optional[Exception] = None
        for attempt in range(self.retries):
            try:
                data = self._get(seg.url).content
                if seg.key:
                    data = self._decrypt(data, seg)
                ts_path.write_bytes(data)
                return ts_path
            except Exception as e:
                last_err = e
                if attempt < self.retries - 1:
                    time.sleep(1.0 * (attempt + 1))

        raise RuntimeError(f"片段 {seg.index} 下载失败（{self.retries} 次重试）: {last_err}")

    def _decrypt(self, data: bytes, seg: Segment) -> bytes:
        try:
            from Crypto.Cipher import AES
        except ImportError:
            raise ImportError("加密流需要安装 pycryptodome: pip install pycryptodome")

        key_data = self._fetch_key(seg.key.uri)
        iv = seg.key.iv if seg.key.iv else seg.index.to_bytes(16, "big")
        cipher = AES.new(key_data, AES.MODE_CBC, iv)
        return cipher.decrypt(data)

    def _fetch_key(self, uri: str) -> bytes:
        with self._lock:
            if uri in self._key_cache:
                return self._key_cache[uri]
        data = self._get(uri).content
        with self._lock:
            self._key_cache[uri] = data
        return data

    # ------------------------------------------------------------------
    # Progress display
    # ------------------------------------------------------------------

    def _on_segment_done(self, index: int):
        with self._lock:
            self._completed += 1
            completed = self._completed
        total = len(self.segments)
        pct = completed / total * 100
        bar_len = 40
        filled = int(bar_len * completed / total)
        bar = "█" * filled + "░" * (bar_len - filled)
        print(f"\r  [{bar}] {completed}/{total}  ({pct:.1f}%)", end="", flush=True)

    # ------------------------------------------------------------------
    # Mode 1: Batch download → merge → convert
    # ------------------------------------------------------------------

    def _run_batch(self):
        """下载全部片段 → 合并 → 转换格式"""
        print(f"\n[批量模式] 第一步：并行下载所有 TS 片段")
        errors: List[str] = []

        with ThreadPoolExecutor(max_workers=self.threads) as executor:
            future_map = {executor.submit(self._download_segment, seg): seg for seg in self.segments}
            for future in as_completed(future_map):
                seg = future_map[future]
                try:
                    future.result()
                    self._on_segment_done(seg.index)
                except Exception as e:
                    errors.append(str(e))
                    print(f"\n  [警告] {e}")

        if errors:
            print(f"\n[警告] {len(errors)} 个片段下载失败，继续合并其余片段...")

        print(f"\n[批量模式] 第二步：合并并转换为 {self.output_format.upper()}")
        filelist = self._write_filelist(self.temp_dir)
        self._ffmpeg_concat_convert(filelist, self.output)

    # ------------------------------------------------------------------
    # Mode 2: Download → immediately convert → merge
    # ------------------------------------------------------------------

    def _run_instant(self):
        """下载每段 → 立即转换 → 最后合并"""
        converted_dir = self.temp_dir / "converted"
        converted_dir.mkdir(exist_ok=True)
        ext = self._intermediate_ext()

        print(f"\n[即时模式] 下载并即时转换各片段为 .{ext}")

        def _worker(seg: Segment) -> Path:
            ts_path = self._download_segment(seg)
            out_path = converted_dir / f"seg_{seg.index:06d}.{ext}"
            self._ffmpeg_convert_single(ts_path, out_path)
            self._on_segment_done(seg.index)
            return out_path

        errors: List[str] = []
        with ThreadPoolExecutor(max_workers=self.threads) as executor:
            future_map = {executor.submit(_worker, seg): seg for seg in self.segments}
            for future in as_completed(future_map):
                seg = future_map[future]
                try:
                    future.result()
                except Exception as e:
                    errors.append(str(e))
                    print(f"\n  [警告] {e}")

        if errors:
            print(f"\n[警告] {len(errors)} 个片段处理失败，继续合并其余片段...")

        print(f"\n[即时模式] 合并所有已转换片段...")
        filelist = self._write_filelist(converted_dir, ext=ext)

        # Final concat: streams are already in target format, just copy
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(filelist),
            "-c", "copy",
        ]
        if self.output_format == "mp4":
            cmd += ["-movflags", "+faststart"]
        cmd.append(str(self.output))
        self._run_ffmpeg(cmd)

    # ------------------------------------------------------------------
    # ffmpeg helpers
    # ------------------------------------------------------------------

    def _ffmpeg_concat_convert(self, filelist: Path, output: Path):
        """Concat TS list and convert to final format in one pass."""
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(filelist),
        ]
        cmd += self._codec_args()
        if self.output_format == "mp4":
            cmd += ["-movflags", "+faststart"]
        cmd.append(str(output))
        self._run_ffmpeg(cmd)

    def _ffmpeg_convert_single(self, ts_path: Path, out_path: Path):
        """Convert a single TS segment to intermediate format (silent)."""
        cmd = ["ffmpeg", "-y", "-i", str(ts_path)] + self._codec_args() + [str(out_path)]
        self._run_ffmpeg(cmd, quiet=True)

    def _codec_args(self) -> List[str]:
        fmt = self.output_format
        if fmt == "mp4":
            return ["-vn", "-c:a", "copy"] if self.audio_only else ["-c", "copy"]
        elif fmt in ("m4a", "aac"):
            return ["-vn", "-c:a", "copy"]
        elif fmt == "mp3":
            return ["-vn", "-c:a", "libmp3lame", "-q:a", "2"]
        else:
            return ["-c", "copy"]

    def _intermediate_ext(self) -> str:
        """Extension used for per-segment converted files in instant mode."""
        if self.output_format == "mp3":
            return "mp3"
        if self.output_format in ("m4a", "aac"):
            return self.output_format
        return "mp4"

    def _run_ffmpeg(self, cmd: List[str], quiet: bool = False):
        kwargs: Dict = {}
        if quiet:
            kwargs["stdout"] = subprocess.DEVNULL
            kwargs["stderr"] = subprocess.DEVNULL
        else:
            print(f"  [ffmpeg] {' '.join(cmd)}")
        result = subprocess.run(cmd, **kwargs)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg 失败，返回码: {result.returncode}，命令: {' '.join(cmd)}")

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _write_filelist(self, directory: Path, ext: str = "ts") -> Path:
        """Write an ffmpeg concat filelist, sorted by segment index."""
        filelist = self.temp_dir / "filelist.txt"
        with open(filelist, "w", encoding="utf-8") as f:
            for seg in sorted(self.segments, key=lambda s: s.index):
                p = directory / f"seg_{seg.index:06d}.{ext}"
                if p.exists():
                    f.write(f"file '{p.absolute()}'\n")
        return filelist

    def _get(self, url: str, **kwargs) -> requests.Response:
        resp = requests.get(url, headers=self.headers, timeout=30, **kwargs)
        resp.raise_for_status()
        return resp


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="HLS/M3U8 多线程下载、合并与格式转换工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s https://example.com/playlist.m3u8 -o video.mp4
  %(prog)s https://example.com/playlist.m3u8 -o audio.m4a --audio-only
  %(prog)s https://example.com/playlist.m3u8 -o video.mp4 --threads 8
  %(prog)s https://example.com/playlist.m3u8 -o audio.mp3 --audio-only --mode instant
  %(prog)s https://example.com/playlist.m3u8 -o video.mp4 --header "Referer:https://example.com"
        """,
    )
    parser.add_argument("url", help="M3U8 播放列表 URL")
    parser.add_argument("-o", "--output", required=True,
                        help="输出文件路径（扩展名决定格式，如 video.mp4 / audio.m4a / audio.mp3）")
    parser.add_argument("-t", "--threads", type=int, default=4,
                        help="并发下载线程数（默认: 4）")
    parser.add_argument("--mode", choices=["batch", "instant"], default="batch",
                        help="batch=全部下载后合并转换（默认）; instant=下载后立即转换再合并")
    parser.add_argument("--audio-only", action="store_true",
                        help="仅提取音频（与 --output *.mp4 配合使用时去掉视频轨）")
    parser.add_argument("--temp-dir",
                        help="临时文件目录（默认: 自动生成，完成后自动删除）")
    parser.add_argument("--retries", type=int, default=3,
                        help="单片段下载失败重试次数（默认: 3）")
    parser.add_argument("--header", action="append", dest="headers",
                        metavar="KEY:VALUE",
                        help="自定义 HTTP 请求头，可多次使用，例如 --header 'Referer:https://x.com'")

    args = parser.parse_args()

    extra_headers: Dict[str, str] = {}
    if args.headers:
        for h in args.headers:
            if ":" in h:
                k, v = h.split(":", 1)
                extra_headers[k.strip()] = v.strip()

    downloader = HLSDownloader(
        url=args.url,
        output=args.output,
        threads=args.threads,
        mode=args.mode,
        audio_only=args.audio_only,
        temp_dir=args.temp_dir,
        retries=args.retries,
        extra_headers=extra_headers or None,
    )

    try:
        downloader.run()
    except KeyboardInterrupt:
        print("\n[HLS] 已中断")
        sys.exit(1)
    except Exception as e:
        print(f"\n[错误] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
