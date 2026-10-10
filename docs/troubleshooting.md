# 部署與使用問題處理

本專案主要由 AI 製作，經人工核驗；本教程由 AI 協助整理，實機打印結果由使用者核對。核驗範圍見[驗證紀錄](verification.md)。

除另有說明，命令在 Docker 主機的專案目錄執行，OpenWrt 主機命令以 root 執行。`/mnt/docker` 是範例及檢查腳本的預設；UUID 必須使用自己的檔案系統 UUID。實測範圍見[驗證紀錄](verification.md)。

| 問題 | 程序處理 |
| --- | --- |
| 無顯示環境時 LibreOffice 失敗 | 固定 `svp` headless 後端 |
| 二進位存在但不能排版 | 啟動時實際生成 Writer／Calc／Impress 預覽，成功才啟動網頁 |
| UNO 未提供 `IsValid` | 精確核對打印機名稱；明確無效時拒絕 |
| Office 失敗後殘留預覽／程序 | 清除失敗預覽、暫存及程序，保留原稿 |
| Office 打印漏進階選項 | 原稿經原生打印及 CUPS 選項橋接提交 |
| 相近型號誤配驅動 | 只自動採用精確型號或設備宣告的通用語言 |
| 切換方式影響共用設定 | 固定模式隊列，依目前方式驗證能力 |
| Cookie 失效／超限／取消誤保存 | 套用才保存，分方式保存，清除失效值及裁減超限資料 |
| 設備警告被翻成錯誤 | 精確區分 warning、report 和 error |
| OpenWrt 沒有 Buildx | 官方臨時 CLI 配合本機 Unix socket |
| 永久掛載／啟動保護失效 | 儲存 guard 和部署前檢查拒絕繼續 |

## OpenWrt 永久儲存及首次安裝

磁碟、分區和檔案系統容量不同，OpenWrt 映像不一定自動擴容。若沒有 `block`，先執行 `apk update` 和 `apk add block-mount`。再核對：

```sh
cat /etc/openwrt_release
uname -m
cat /proc/partitions
cat /proc/mounts
block info
df -h / /mnt/docker
```

需調整分區時先備份資料、分區表及開機設定。根 ext4 線上擴容失敗可能需要根分區未掛載的維修環境；已有尾端資料分區時不能直接套用根分區擴到磁碟末端的腳本。日常更新不重新分區或格式化。

準備永久 ext4 分區，在 `/etc/config/fstab` 登記為 `docker`。使用 `block info` 顯示的實際檔案系統 UUID，不能填 GPT PARTUUID；保留其他掛載設定。以下不建立或格式化分區：

```sh
uci set fstab.docker=mount
uci set fstab.docker.target='/mnt/docker'
uci set fstab.docker.uuid='<自己的 ext4 檔案系統 UUID>'
uci set fstab.docker.fstype='ext4'
uci set fstab.docker.enabled='1'
uci commit fstab
block mount
```

核對 `/mnt/docker` 是預期可寫 ext4 掛載，再建立資料目錄及複製專案。OpenWrt 25.12 使用 `apk` 和 OpenWrt 自帶來源，不加入 Alpine 來源：

```sh
apk update
apk add block-mount dockerd docker docker-compose
/etc/init.d/dockerd stop
mkdir -p /mnt/docker/data
uci set dockerd.globals.data_root='/mnt/docker/data'
uci commit dockerd
```

這是全新 Docker 的設定。已有映像／容器／資料卷時，先安全移轉，不能只改 `data_root`。

### Docker 儲存啟動保護

在專案目錄執行：

```sh
sh deploy/host/storage-ready.sh
mkdir -p /usr/libexec
cp deploy/host/storage-ready.sh /usr/libexec/fpc-docker-storage-ready
chmod 755 /usr/libexec/fpc-docker-storage-ready
cp -p /etc/init.d/dockerd /etc/init.d/dockerd.before-fpc
```

編輯 `/etc/init.d/dockerd`，在 `start_service()` 第一個有效行加入：

```sh
/usr/libexec/fpc-docker-storage-ready || { logger -t dockerd "Docker storage unavailable; refusing start"; return 1; }
```

guard 核對 ext4／可寫、`fstab.docker.uuid`、資料目錄位置，拒絕符號連結逃出和 `alt_config_file` 覆蓋。只改開機順序不能防磁碟遺失；掛載失敗拒絕啟動，避免在根分區建立另一套資料。

```sh
/etc/init.d/dockerd enable
/etc/init.d/dockerd start
docker info
docker compose version
sh deploy/host/preflight.sh
```

自訂位置須讓主機配置、更新和開機 guard 都使用同一組 `FPC_DOCKER_MOUNT`、`FPC_DOCKER_DATA_ROOT`、`FPC_DOCKER_UUID`；單次終端匯出不會替開機服務保存。套件升級可能替換啟動檔，之後重新核對保護行。`preflight.sh` 預設至少 4 GiB 剩餘空間，初次安裝及更新備份建議更多。

目錄存在不代表已掛載。掛載失敗期間若產生另一套資料，先停止 Docker、保存及辨識兩套資料再恢復。唯讀或 I/O 錯誤先檢查磁碟／檔案系統。

## 建置連不上套件來源

先檢查 Docker、DNS 和轉送。bridge 容器的 `127.0.0.1` 是容器自身，不能當作 OpenWrt DNS：

```sh
uci -q get dockerd.globals.dns
uci show firewall
docker info
```

缺少相關配置時可使用以下範例；已有清單項不要重複加入：

```sh
print_center_lan_ip="$(uci -q get network.lan.ipaddr)"
print_center_lan_ip="${print_center_lan_ip%%/*}"
test -n "$print_center_lan_ip"
uci add_list dockerd.globals.dns="$print_center_lan_ip"
uci commit dockerd
/etc/init.d/dockerd uciadd
uci add_list firewall.docker.device='docker0'
uci set firewall.fpc_docker_wan=forwarding
uci set firewall.fpc_docker_wan.src='docker'
uci set firewall.fpc_docker_wan.dest='wan'
uci commit firewall
/etc/init.d/firewall reload
/etc/init.d/dockerd restart
```

`docker → wan` 供主動下載，不開放 WAN 連入網頁。Docker 重啟會影響其他容器，請選適當時機。官方 Hub 不可達可改 README 的 ECR Public 來源，但換來源不能修復 DNS／防火牆。

## 網頁或 SSH 連不到

使用實際主機 LAN 位址，連上家庭網路而非隔離訪客 Wi-Fi，外網透過自己已有的家庭 VPN。不要為了更新開放 WAN SSH、8080 或 631。

`PRINT_CENTER_HOST=127.0.0.1` 只供本機，手機需要填主機 LAN 位址並執行 `docker compose up -d --no-build`：

```sh
docker compose ps
docker compose logs --tail=100
ss -lnt
```

保持 WAN 輸入拒絕。若 LAN 也拒絕，可加只供 LAN 的規則，埠號與 `.env` 一致：

```sh
uci set firewall.family_print_lan=rule
uci set firewall.family_print_lan.name='Allow-Family-Print-LAN'
uci set firewall.family_print_lan.src='lan'
uci set firewall.family_print_lan.proto='tcp'
uci set firewall.family_print_lan.dest_port='8080'
uci set firewall.family_print_lan.target='ACCEPT'
uci commit firewall
/etc/init.d/firewall reload
```

zone 可能是匿名 UCI 項，不一定叫 `firewall.wan`；用 `uci show firewall` 的 `.name='wan'` 辨認真正區域。

## 網頁卡在搜尋打印機／Too many open files

若首頁偶爾可開，但打印機、任務或 JavaScript 載入逾時，先查 `docker compose logs --tail=100`。實機曾出現 `OSError: [Errno 24] Too many open files`：pycups 2.0.4 的 PPD 批次 getter 保留原生引用，反覆讀取能力使已刪除的 PPD 暫存檔仍占用檔案描述符；Python 垃圾回收無法釋放它們。

程式已將 PPD 能力解析及選項衝突檢查移至短生命子程序，結束時回收原生資源。子程序只回傳 JSON，不連線或送印；解析失敗保留 IPP 能力，送印前驗證逾時或失敗則拒絕提交。取得 PPD 但無法解析，也不能視為驗證成功。

更新程式並重新建置映像可持續修正此問題。重啟容器只會暫時釋放舊資源；更新或重啟前先用 `docker exec family-print-center lpstat -W not-completed -o` 查看待印任務，確認列出的工作已結束。不要把提高描述符上限或定時重啟當成修復。

## 小 PDF 卻提示超過上傳上限

舊版表單緩衝上限為 64 KiB，等於 Werkzeug 每次讀取大小；換行跨越讀取邊界時，保留位元組會使解析器觸發 HTTP 413，即使 PDF 只有數 MB。此錯誤曾被誤顯示為超過 100 MB。

程式已將表單緩衝調為 512 KiB，檔案仍串流寫入並受 `MAX_UPLOAD_MB` 限制；表單欄位過大或過多的錯誤也會顯示獨立原因。更新並重新建置映像，再重選文件；不需為此提高檔案上限。真正超過檔案上限的內容仍會被拒絕。

## 紙張、頁邊距與預覽頁碼

在預覽上方的文件選單或左右按鈕切換文件；頁碼框輸入 1 到總頁數的整數，按 Enter 或離開輸入框跳頁。Esc 取消編輯，非法頁碼不跳轉，背景更新不覆蓋正在輸入的文字。文件仍在處理或失敗時，可切換到其他文件。

「更多設定 → 紙張」集中管理尺寸和四邊頁邊距。紙張預設自動，只有打印機提供自訂尺寸範圍時才可編輯寬、高；其他尺寸只讀顯示毫米數值。使用「自訂頁邊距」修改四邊，按套用後才生效並記入偏好 Cookie。

頁邊距作用於列印，文件預覽保持原版面；Word／Excel 可能重新分頁。PDF、圖片和電子書保留整頁比例縮入內容區域，不重排文字。此時額外縮放選自動或原始大小；無法確認實際紙型尺寸時，改選 A4 等明確尺寸再調頁邊距。內容區須至少 1 mm 寬、高，打印機的物理不可列印區仍有效。

PowerPoint 原稿不接受四邊頁邊距覆蓋，因為 LibreOffice 的投影片邊界屬性不會可靠地縮入所有圖形。請保留自動，或由原軟體另存 PDF 再上傳；程序不會自動把 Office 預覽 PDF 改作打印來源。含投影片的多文件批次會先檢查此限制，不先印出前面的文件再失敗。

## 容器健康但首頁或靜態資源無法讀取

先檢查首頁與 `/static/app/core.js`，不要只看 `/healthz`。若日誌出現模板或靜態資源的 `PermissionError`，以應用帳號核對目錄是否可遍歷：

```sh
docker exec -u familyprint family-print-center python -c 'from pathlib import Path; print(Path("/app/print_center/templates/index.html").stat()); print(Path("/app/print_center/static/app/core.js").read_text()[:40])'
```

來源壓縮包若只含文件、缺少目錄條目，在 `umask 077` 下解壓會建立 `0700` 的 root 目錄，即使文件本身是 `0644`，應用帳號仍不能讀取。新版發布工具明確保存公開目錄的 `0755` 權限；部分 BusyBox 版本仍會套用 umask，因此解壓公開來源到獨立暫存目錄時使用 `umask 022`，並檢查結果。Dockerfile 亦確保公開應用目錄可讀、可遍歷；修正來源後重建並重新啟用容器，不以臨時修改運行容器代替建置修正。此權限處理僅針對公開程式，不能對資料卷、`.env` 或憑證目錄套用。

新版健康檢查同時請求首頁、CSS 與 JavaScript 入口；它仍不代替所有瀏覽器交互或實體打印測試。

OpenWrt 的精簡 BusyBox `tar` 不一定提供 `--strip-components`。本專案來源包包含一層 `family-print-center/` 目錄，先解壓到獨立暫存目錄，再從該目錄同步允許發布的文件。更新既有部署時保留 `.env`、本機字體快取及所有資料卷，並先備份。

## LibreOffice 啟動／預覽／選打印機失敗

程式固定 `svp`，不需 X11。啟動以普通應用帳號生成三種 Office 預覽，成功記錄在本次 `/run`，失敗不啟動網頁。健康檢查讀此記錄，並請求首頁及前端入口資源，不每 30 秒重啟 LibreOffice：

```sh
docker compose logs --tail=100
docker compose exec -u familyprint print-center python /opt/print-center/doctor.py
```

完整診斷真正轉換生成的文件，不提交打印。`--quick`、二進位存在、首頁可開都不能證明 Office 排版成功。核對可寫儲存、UNO／LibreOffice 依賴及帳號權限，不手工建立成功記錄。

某些 UNO 版本沒有 `IsValid`，程序此時精確核對打印機名稱；明確無效拒絕提交。原始 Office 仍經 LibreOffice 原生打印；CUPS 收到 PDF 可以是 LibreOffice 內部輸出，不能據此認為用了預覽 PDF。

## 驅動或模式缺少功能

功能依已安裝驅動和設備回報。IPP 連線不等於免驅動，亦不保證全部廠商功能。採用相符驅動、明確通用語言或已確認 IPP；完整型號的廠商程式及依賴需建置進映像，不能只上傳含過濾程式的 PPD。

更新來源不自動改舊隊列；後台修改驅動時保留現有可用設定。現有 DNS-SD 隊列可先唯讀檢查，將 `<隊列名稱>` 換成實際名稱：

```sh
docker exec -u familyprint family-print-center python -m print_center.printer_profiles_cli --printer '<隊列名稱>'
```

工具核對現有 DNS-SD UUID、唯一探索身份及連線一致，提出模式建議，不需在來源填實機 UUID。確認後加 `--apply` 建立／登記，再重啟容器載入映射；不安裝程式、不送印。

通用 PCL 雙面器可能預設未安裝。對已知的 `brother_mfc8515dn` 隊列，遷移工具核對設備雙面能力和精確 Generic PCL PPD，才啟用確認的安裝選項，保留其他內容、讀回驗證，失敗回復；其他設備的安裝選項須另外核對。驅動和 IPP 各自固定隊列，家庭頁只顯示一台邏輯打印機。

## A4 普通紙雙面提示紙張不支援

核對實際裝紙、面板尺寸／種類、CUPS 隊列預設和單次選項。A4 無邊界、相片紙和 A4 普通紙不同，可能不允許相同雙面方式。

Epson L6276 實測面板和 `media-col-ready` 為 A4／普通紙，工廠 `media-default` 及 PPD 預設卻仍是 Letter。首次工作雖指定 A4，設備拒絕雙面。保存原 PPD、把 `DefaultPageSize`／`DefaultPageRegion` 對齊 A4，並明確選普通紙，再以 A4、300 DPI、長邊雙面重試成功。兩項條件同時更改，不能唯一歸因 Letter。

`自動` 不送該項參數。普通紙在「更多設定 → 紙張」選擇後須套用。隊列預設影響共用者，修改前保存原配置，之後讀回核對。

已知 Epson L6270 Series 免驅動隊列可顯式檢查對齊已裝 A4：

```sh
docker exec -u familyprint family-print-center python -m print_center.printer_profiles_cli --printer epson_l6276 --align-ready-paper
```

確認唯讀結果後：

```sh
docker exec -u familyprint family-print-center python -m print_center.printer_profiles_cli --printer epson_l6276 --align-ready-paper --apply
docker restart family-print-center
```

此特殊修復限定已知 Epson 隊列名稱和 L6270 Series 免驅動型號。核對唯一 DNS-SD 身份、直接 IPP UUID 相同、`media-ready` 唯一 A4、工廠 `media-default` Letter，以及 PPD 尺寸定義及預設。只有加 `--apply` 才改，其他型號／紙型不推測，已對齊時不重複改；未終止作業阻擋，保留其他 PPD 內容、驗證及回復。預設 `--apply` 不改紙張。工具不驗證普通紙，仍需面板及單次選項確認。

仍有提示時先讀面板原文和網頁狀態，處理紙張或清洗，不連續重送未完成工作。取消後空白或單面紙也計入用紙。設備紙數計數可能不準，實際張數、正反內容和方向人工確認。

## 字體、版面或清晰度不同

缺少原字體、LibreOffice 差異或文件打印範圍均可能影響版面。後台「字體」查看缺少名稱，上傳有使用權字體，或同步 `deploy/fonts/private/` 重新建置。公開 GitHub 不含自己的字體包，部署不能只複製 Git 文件。

快取核對 SHA-256，下載失敗可能略過字體，檢查建置訊息及 `fc-match`。安裝後移除再上傳文件產生新預覽。Excel 原軟體先核對工作表、範圍及分頁；必要時自行匯出 PDF。紙型／縮放／多頁仍須實印核對。

預覽清楚而紙面模糊，分別檢查品質、紙張、清洗及對齊；硬體校準由面板／廠商工具處理，程序測試不能替代。

## 偏好沒有記住

按瀏覽器、站點主機名稱、打印機及方式保存，IP／主機名稱、不同裝置、私密視窗不共用。清除資料重置，能力變動使失效值恢復自動。進階必須套用，取消不保存，超限裁減較舊偏好。重整同一網址核對選項；文件及頁碼不靠 Cookie 恢復。

## 忘記管理員密碼

由主機管理員重設：

```sh
docker exec family-print-center rm /var/lib/family-print-center/admin.json
docker restart family-print-center
docker exec family-print-center cat /var/lib/family-print-center/admin-initial-password
```

新密碼登入後更改，隊列／文件保留。不要貼密碼或 `.env` 到公開 issue。

## 更新後驗證

1. 核對掛載及資料位置，OpenWrt 執行 `sh deploy/host/preflight.sh`。
2. 同步來源及自己的字體，保留 `.env`／資料卷，建置更新。
3. 查看狀態、日誌及完整 `doctor.py`。
4. 核對 Office／PDF 預覽、能力及 Cookie。
5. 新功能才依用紙預算實印，分別記錄 CUPS 終止及紙面確認。

公開報告去除實機 UUID、內網 IP、文件名稱、憑證及家庭資料；保留版本、方式、選項及錯誤原文。
