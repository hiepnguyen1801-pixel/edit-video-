# Edit video hàng loạt cho TikTok

Script `tiktok_batch.py` dùng **ffmpeg** để xử lý cả thư mục video, xong video này thì tự chạy video tiếp theo. Nếu một video bị lỗi, script ghi log và chạy tiếp các video còn lại.

## Cài đặt

```bash
# Ubuntu/Debian
sudo apt install ffmpeg python3-pip
# macOS: brew install ffmpeg
# Windows: tải ffmpeg rồi thêm vào PATH
pip install -r requirements.txt
```

## Chạy

```
input/   <- video gốc (mp4, mov, mkv, ...)
music/   <- nhạc nền (mp3, m4a, wav ...), bạn tự tải
output/  <- kết quả *_tiktok.mp4
```

```bash
python3 tiktok_batch.py -i input -o output -m music
# Chạy song song 2 video, có lật ngang:
python3 tiktok_batch.py -i input -o output -m music -j 2 --flip
```

Video nào đã có trong `output/` sẽ được bỏ qua, nên khi bị dừng giữa chừng chỉ cần chạy lại lệnh. Muốn render lại thì thêm `--overwrite`.

## Yêu cầu và cách xử lý

| Yêu cầu | Cách làm | Tham số |
|---|---|---|
| 1080x1920, 9:16 | scale + nền mờ (blur) | `--layout blur\|safe\|fill` |
| Safe box 880x1420 | `--layout safe` thu nội dung vào khung 880x1420 ở giữa | |
| 30 fps, H.264, MP4 | libx264 high, yuv420p, faststart | |
| Bitrate 5-8 Mbps | trung bình 6 Mbps, tối đa 8 Mbps | `--bitrate 6 --maxrate 8` |
| Dynamic zoom 102-105% | zoom theo hình sin, chu kỳ và pha ngẫu nhiên cho từng video | `--zoom 1.02-1.05` |
| Contrast +3%, Brightness -2% | `eq=contrast=1.03:brightness=-0.02` | `--contrast --brightness` |
| Tốc độ 1.04x | `setpts=PTS/1.04` | |
| Thanh tiến trình | thanh chạy từ 0 đến 100% theo thời lượng | `--bar top\|bottom`, `--bar-color` |
| Hạt nhiễu 2-3% | `noise` cường độ khoảng 2.5% | `--grain 0.025` (0 = tắt) |
| Lật ngang | chỉ bật khi thêm cờ | `--flip` |
| Âm thanh 1.04x, không giữ cao độ | `asetrate` làm cao độ tăng khoảng +0.68 semitone | |
| Nhạc nền 3-5% | chọn ngẫu nhiên 1 bài trong `music/`, lặp lại, fade cuối | `--bgm-volume 0.03-0.05` |
| Cắt 1-2s đầu/cuối | độ dài cắt ngẫu nhiên trong khoảng | `--trim-head 1-2 --trim-tail 1-2` |
| Hook 3s lên đầu | lấy đoạn 3s có năng lượng âm thanh mạnh nhất đưa lên đầu | `--hook 3 --hook-mode move\|copy` |
| Auto-cut khoảng lặng | `silencedetect`, ngưỡng -35dB, đoạn lặng ≥ 0.6s | `--silence-db --silence-min --silence-pad` |

Ghi chú:
- `--layout blur` (mặc định): video ngang nằm giữa, nền là chính video đó làm mờ. `--layout safe`: toàn bộ nội dung nằm trong safe box 880x1420. `--layout fill`: crop cho kín khung.
- `--hook-mode move` cắt đoạn hook khỏi vị trí cũ. `copy` thì vừa đặt hook lên đầu vừa giữ đoạn đó ở chỗ cũ.
- Video không có tiếng sẽ không cắt khoảng lặng và lấy hook ở khoảng 35% thời lượng.
- Muốn chạy nhanh hơn dùng `--preset veryfast` (file lớn hơn một chút).
- Thời gian xử lý tham khảo: khoảng 1.5-3 lần độ dài video với preset `medium`, tùy CPU.

## Phần mở rộng

```bash
python3 tiktok_batch.py -i input -o output -m music -b broll \
    --blur-box 0,0.78,1,0.17 --subs auto --lang vi
```

| Tính năng | Cách làm | Tham số |
|---|---|---|
| Làm mờ phụ đề/logo gốc | boxblur mạnh lên từng vùng, tọa độ theo tỉ lệ khung gốc `X,Y,W,H` (0..1). Lặp lại tham số được nhiều vùng | `--blur-box 0,0.78,1,0.17` (dải phụ đề dưới) `--blur-box 0.8,0,0.2,0.1` (logo góc phải trên) |
| Phụ đề động | hiện từng cụm 3 từ, từ đang đọc sáng vàng và phóng to, nằm trong safe box | `--subs auto\|srt\|off`, `--font`, `--font-size`, `--no-upper` |
| Chớp sáng / Glitch / B-roll 0.5s | cứ 4-8 giây chèn 1 hiệu ứng ngẫu nhiên, ưu tiên đặt đúng điểm cắt | `--fx-every 4-8` (0 = tắt), `-b broll/`, `--broll-len 0.5` |
| EQ cắt bass/treble | high-pass 90 Hz, low-pass 13 kHz | `--eq-low 90 --eq-high 13000` (0 = tắt) |
| Reverb < 5% | aecho 3 tầng, mức tối đa 0.05 | `--reverb 0.04` |

Phụ đề:
- Nếu bên cạnh video có file `.srt` trùng tên (ví dụ `abc.mp4` + `abc.srt`), script dùng file đó và tự khớp thời gian sau khi cắt/tăng tốc.
- `--subs auto` không có `.srt` thì dùng Whisper (`pip install faster-whisper`, lần đầu tự tải model, `--whisper-model small`). Thêm `--lang vi` cho tiếng Việt.
- Font phải hỗ trợ tiếng Việt (Arial, Roboto, Be Vietnam Pro...).

Thư mục `broll/` chứa ảnh (jpg/png) hoặc clip ngắn, mỗi lần chèn dài 0.5 giây.

Chỉ dùng cho video và nhạc mà bạn có quyền sử dụng.
