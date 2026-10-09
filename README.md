# 家庭打印中心

**本專案主要由 AI 製作，經人工核驗。** 程式、介面、文件及測試主要由 AI 協助完成；使用者在實機上確認打印張數、份數、雙面與翻頁方向等結果。核驗範圍及未測項目見[驗證紀錄](docs/verification.md)。

以 Python、Flask、CUPS 和 LibreOffice 建立的家庭網頁打印中心。手機和電腦連上家庭網路即可上傳、預覽及打印，不需登入；新增打印機、驅動、字體及設備管理使用有密碼保護的 `/admin` 後台。

CUPS、LibreOffice、字體和打印驅動運行在 Debian 容器內，OpenWrt 只負責 Docker 主機。介面採用大字、留白及游標反色入口，支援手機、鍵盤和減少動態效果。網頁字體本地提供，不依賴 CDN。

## 功能

- PDF、Office、圖片及電子書逐頁預覽，可切換文件、輸入頁碼跳頁，每份文件保留本次查看的頁碼，也可另外開啟 PDF。
- Office 由 LibreOffice 開啟原始文件並直接打印；PDF 預覽不作為 Office 的打印來源。
- 一次最多加入 20 份文件，逐份上傳及處理，共用一組打印設定；指定頁碼只適用於單一文件。
- 基本打印設定加進階設定，能力依目前打印機及打印方式產生。
- 自動探索 Bonjour／DNS-SD 和 CUPS 打印機，或手動加入家庭 LAN 位址。
- 有確認相符的驅動時優先使用驅動，另提供免驅動 IPP 相容方式。
- 按瀏覽器、打印機及方式記住設定；顯示任務、墨水／碳粉和缺少字體提示。
- 文件預設保留 24 小時，可手動移除，處理及儲存都在打印中心本機。

| 類別 | 支援副檔名 |
| --- | --- |
| PDF | `pdf` |
| Word／文字 | `doc`、`docx`、`docm`、`odt`、`rtf`、`txt`、`md` |
| Excel／表格 | `xls`、`xlsx`、`xlsm`、`ods`、`csv`、`tsv` |
| PowerPoint | `ppt`、`pptx`、`pptm`、`ppsx`、`odp` |
| 圖片 | `jpg`、`jpeg`、`png`、`gif`、`bmp`、`tif`、`tiff`、`webp` |
| 電子書／其他 | `epub`、`fb2`、`cbz`、`xps`、`oxps` |

Office 巨集停用；Markdown 當作純文字處理，GIF／WebP 動畫使用第一幀，TIFF 支援多頁。電子書、圖片及 XPS 等會轉成 PDF 再打印。其他專有格式可先由原始軟體匯出 PDF。

LibreOffice 和 Microsoft Office 的排版可能不同。Excel 沿用文件的工作表、打印範圍及分頁設定；CSV 沒有這些版面設定。密碼保護文件須先解除密碼。

## 打印設定

主頁提供打印機、份數、頁碼、單／雙面、彩色／黑白、紙張大小及方向。「列印效果」可快速套用一般、省墨、精美或相片設定，只使用目前打印機有的選項。

「更多設定」包含以下分頁，實際項目以目前隊列能力為準：

| 分頁 | 項目 |
| --- | --- |
| 紙張 | 紙張大小、毫米寬高、自訂尺寸、四邊頁邊距、紙張種類、進紙紙匣 |
| 品質 | DPI、列印品質、省墨及相關驅動選項 |
| 版面 | 縮放、每張多頁、框線、奇偶頁、出紙順序、小冊子、鏡像 |
| 其他 | 打印方式、出紙位置、裝訂／打孔及其他單次打印選項 |

可自動選擇的項目初次預設為「自動」：程序不送出該項參數，沿用文件及隊列預設。份數預設 1，頁碼留空表示全部。進階視窗按「套用設定」才保存，取消或 Esc 不保存；「全部改回自動」可還原設定。

紙張選單依打印機實際支援的尺寸分為常用紙張、信封與明信片、相片紙及其他尺寸，顯示毫米寬高並區分無邊框紙型。頁面設定參考 [Word 的自訂頁邊距介面](https://support.microsoft.com/en-us/word/create-custom-margins)，提供自動、一般（四邊 25.4 mm）、窄（四邊 12.7 mm）及自訂。上、下、左、右各可設 0–100 mm，內容區須至少保留 1 mm 寬、高；零值不代表打印機具備無邊框能力。

Word／Excel 的頁邊距套用到 LibreOffice 內使用的頁面樣式，原檔不改寫，仍直接打印 Office 文件；未指定紙張時保留文件原有尺寸。PDF、圖片及電子書將每頁等比例縮入指定紙張的內容區域，保留原始上傳檔案。PowerPoint 沿用原版面，若需指定四邊頁邊距，請自行另存 PDF 再上傳；程序會在提交前拒絕無法套用的設定。已指定頁邊距時，額外縮放請保持自動或原始大小，以免再次縮放破壞頁邊距。

「其他 → 打印方式」預設自動，優先使用確認相容的已安裝驅動。有相容性問題時可切換「免驅動 IPP（相容）」。每種方式使用固定 CUPS 隊列；切換不改寫家人共用的驅動或隊列預設。DPI、紙匣及品質依所選方式重新建立，不代表設備的物理最高解析度。

偏好 Cookie 有效一年，保存上次打印機、方式、份數和基本／進階設定；不同打印機及方式分開保存，失效值恢復自動，空間不足時裁減較舊偏好。文件、文件名稱、頁碼及登入資料不放在偏好 Cookie 中。不同瀏覽器、裝置及 IP／主機名稱不共用 Cookie。

預覽顯示文件原版面，打印時才套用紙張、頁邊距、縮放及多頁排列；Word／Excel 修改頁邊距可能重新分頁，因此打印頁數可能與原預覽不同。頁面設定中的小紙張是示意圖。DPI 與品質也可能互相影響。

## Docker 部署

建議使用有永久磁碟儲存的 Linux 主機；已在 OpenWrt 25.12、x86_64、Intel N100、8 GB RAM 環境驗證。首次建置及更新建議預留至少 8 GB 可用空間。OpenWrt 的儲存、DNS、防火牆及 Docker 啟動保護見[部署與問題處理教程](docs/troubleshooting.md)。

在專案目錄執行：

```sh
cp .env.example .env
```

將 `.env` 的 `PRINT_CENTER_HOST` 改為主機家庭 LAN 位址；預設 `127.0.0.1` 只可在主機本機存取。然後：

```sh
sh deploy/build-image.sh
docker compose up -d --no-build
docker compose ps
docker compose logs --tail=80
```

開啟 `http://<主機的 LAN 位址>:8080/`。容器使用主機網路供 Bonjour 探索及打印連線；CUPS 僅監聽 loopback 和容器內 UNIX socket，不開放其網頁管理介面。打印服務不掛載 Docker socket。

建置入口優先使用本機 Buildx；沒有時使用官方 `docker:29-cli` 臨時容器及已確認的本機 Unix socket。遠端 Docker context 不適用此備援。Docker Hub 無法連線時可改用官方 ECR Public 映像：

```sh
DEBIAN_IMAGE=public.ecr.aws/docker/library/debian:trixie-slim \
DOCKER_CLI_IMAGE=public.ecr.aws/docker/library/docker:29-cli \
sh deploy/build-image.sh
```

首次啟動的管理員密碼在主機終端查看：

```sh
docker exec family-print-center cat /var/lib/family-print-center/admin-initial-password
```

登入 `/admin` 後須改成自己的密碼，初始密碼檔會刪除。也可首次啟動前填入 `.env` 的 `ADMIN_PASSWORD`；管理員密碼建立後不再使用此設定。

### 打印機與驅動

後台「新增打印機」提供自動檢測及家庭 LAN IP／名稱查詢。Bonjour 打印機保存 DNS-SD 名稱，DHCP 位址變動時重新解析；沒有 Bonjour 的設備建議設定 DHCP 保留位址。探索需要能接收 mDNS 的同一 LAN，訪客網路、VLAN 或 Wi-Fi 隔離可能阻擋。

自動配對採用完整型號相符的驅動，或設備明確宣告的通用 PCL／PostScript。找不到時採用已確認的免驅動 IPP，不因同品牌或型號相近就自動套用。動態生成的 IPP PPD 歸為免驅動。

映像包含 Gutenprint、HP、Brother brlaser、Epson escpr、splix、foo2zjs、c2esp、Fuji Xerox、min12xxw、pxljr、ptouch 及 OpenPrinting／Foomatic PPD。額外 Debian 套件可在建置時加入：

```sh
EXTRA_DRIVER_PACKAGES="套件1 套件2" sh deploy/build-image.sh
```

後台可上傳 `.ppd`／`.ppd.gz`，每檔最多 2 MB、共最多 30 個；不接受附帶可執行過濾程式的 PPD。廠商過濾程式及依賴須建置進映像，上傳 PPD 不會安裝程式。

USB 打印機須取消 `compose.yaml` 中 `devices` 兩行註解，再執行 `docker compose up -d --no-build` 並檢測。網路打印機不需要 USB 直通。既有隊列模式遷移和雙面器配置見教程。

### 字體

映像提供 Noto CJK、IPA／IPAex、BIZ UD、文鼎、文泉驛、Liberation、Carlito／Caladea 等中、日、英文字體。下載包另含全字庫等字體。常見名稱透過 `deploy/fonts/60-family-print-center.conf` 對應至已安裝字體；原字體可用時優先使用，缺少時採用替代，不能保證完全相同的換行及字形。

非 Debian 套件的來源和固定 SHA-256 在 `deploy/fonts/sources.txt`。`build-image.sh` 下載缺少的包至 `deploy/fonts/cache/`，安裝時再驗證。下載失敗仍可能完成映像，不能只靠建置成功判斷字體齊全。

有使用權的 `.ttf`、`.ttc`、`.otf` 或 `.otc` 可用兩種方式導入：

1. 放入本機 `deploy/fonts/private/`，可分子目錄，重新建置。部署到自己的主機時另行同步 `private/` 和 `cache/`，公開 GitHub 來源不包含它們。
2. 後台「字體」上傳，每檔最多 64 MB、共最多 60 個及 512 MB。存在 `app-data` 資料卷，更新映像後保留，無須重啟。

後台會列出近期 Office 文件缺少的字體。安裝後移除並重新加入文件，才更新舊預覽及提示；原稿打印使用目前字體。容器可核對實際匹配：

```sh
docker compose exec -u familyprint print-center fc-match 'Calibri'
docker compose exec -u familyprint print-center fc-match 'Microsoft JhengHei'
```

使用、複製及再散布須依各字體原始授權確認。自己的 Windows、Office 或其他商業字體不隨公開來源、映像或資料卷發布。本地網頁 Poppins 的授權在 `print_center/static/fonts/OFL-Poppins.txt`。

## 更新與維護

OpenWrt 設好永久掛載和啟動保護後使用：

```sh
sh deploy/host/preflight.sh
sh deploy/host/update.sh
```

`preflight.sh` 唯讀核對 ext4 掛載、UUID、資料位置、啟動 hook、空間及本機 Docker／Compose；`update.sh` 通過後建置更新，不分區、格式化或更改主機網路。一般 Linux 使用 `build-image.sh` 和 Compose。

```sh
docker compose ps
docker compose logs --tail=100
docker compose exec -u familyprint print-center python /opt/print-center/doctor.py
```

`doctor.py` 實際生成 Writer、Calc、Impress 預覽，另檢查 CUPS、儲存和 Bonjour；不讀家庭文件、不提交或取消打印。`--quick` 略過 Office 轉換，不能證明 Office 可用。每次容器啟動也執行三項實際 Office 預覽，全部通過才啟動網頁並允許健康檢查成功。

三個命名資料卷保存應用資料、CUPS 配置及打印隊列。`docker compose down` 保留資料卷，`down -v` 會刪除。更新前備份 `.env`、私有字體及三個資料卷；備份含家庭文件和管理資料，私人保存。

家人可以暫停、繼續或取消打印中心提交的任務。正在打印的任務暫停後恢復，可能從第一頁重新開始；設備已收到的頁面仍可能出紙。已結束任務需重新送出，CUPS 不保留已打印原始 spool 文件。

問題見[處理教程](docs/troubleshooting.md)，實測範圍見[驗證紀錄](docs/verification.md)。

| 設定 | Compose 預設 | 用途 |
| --- | --- | --- |
| `PRINT_CENTER_HOST` | `127.0.0.1` | 監聽位址，部署時填家庭 LAN 位址 |
| `PRINT_CENTER_PORT` | `8080` | 網頁埠號 |
| `MAX_UPLOAD_MB` | `100` | 單份文件上限 |
| `CONVERSION_TIMEOUT` | `180` | 處理逾時秒數 |
| `RETENTION_HOURS` | `24` | 保存時間 |
| `ADMIN_PASSWORD` | 空白 | 首次管理員密碼，空白則生成 |
| `SOURCE_CODE_URL` | 空白 | 對應版本原始碼的 HTTP(S) 網址，設定後頁尾顯示「原始碼」 |

修改 `.env` 後執行 `docker compose up -d --no-build`。容器記憶體上限預設 3 GB，一次處理一份文件並限制等待隊列；須為主機其他服務保留記憶體。

## 存取與安全

保持只在家庭 LAN 使用，不轉發網頁或 CUPS 埠至 WAN。可用同一個 LAN IP 加 `:8080`；獨立名稱或省略埠號須另設家庭 DNS／反向代理，避免與 LuCI 監聽衝突。

網頁以普通帳號運行，管理密碼雜湊保存，登入有次數限制及到期，寫入要求 CSRF 和同站來源。Office 停用巨集、外部連結更新及外部內容，容器限制記憶體／程序，上傳亦有大小限制。家人可操作彼此的打印任務，適用信任的家庭 LAN。

管理後台預設 HTTP；需要時自行配置 HTTPS 反向代理。代理未轉交來源 IP 時，登入次數限制由所有使用者共用。

## 開發與測試

本機需要 Python、CUPS 開發標頭、LibreOffice、UNO 和 `cc`／`gcc`。原生打印使用 `deploy/cups_options_shim.c`；開發環境首次使用時編譯至應用資料目錄，Docker 已包含編譯結果。

```sh
python3 -m venv --system-site-packages .venv
. .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python3 -m pytest
python main.py
```

開發網址 `http://127.0.0.1:18080/`，由 `PORT` 調整；初始密碼在 `data/admin-initial-password`。本機直接啟動的上傳上限預設 50 MB，Compose 預設 100 MB。

Node.js 24 以上可驗證網頁模組：

```sh
node tests/check_browser_preferences.js
node tests/check_upload_queue.js
```

自動測試使用模擬 CUPS 或獨立測試端，不修改家庭隊列、不送實體打印。真實 LibreOffice／socket 測試需要相應依賴及 loopback 連線；受限沙箱失敗須與程序失敗分別判斷。新驅動、紙型及設備功能仍須按驗證範圍核對紙面。

## 公開來源及授權

本專案採 AGPL-3.0-only，全文見 [LICENSE](LICENSE)，第三方說明見 [NOTICE](NOTICE.md)。可設定 `SOURCE_CODE_URL`，供使用者取得對應版本原始碼；自訂修改後應提供該修改版本，而非只連到上游。網址須為完整 HTTP(S) 位址，不包含帳號或密碼。

```sh
python3 tools/prepare_public_release.py --check
python3 tools/prepare_public_release.py
```

工具依允許清單檢查和打包，預設輸出到 `artifacts/public-release/`，排除 `.env`、資料、私有部署紀錄、字體快取及私有字體。公開包不是包含自己字體與設定的主機部署包。發布前核對檔案清單，勿將整個工作目錄或部署備份直接上傳。
