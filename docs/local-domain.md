# 家庭網址與 LuCI 共用既有埠

以下以 `http://print.home.arpa/` 跳轉至 `http://192.168.1.1:8080/` 為例。請換成自己的路由器 LAN 位址。DNS 只對應名稱與 IP；由路由器既有的 uhttpd 回覆 HTTP 302，瀏覽器再開啟打印中心。網址列會變成 IP 與 `:8080`，因此沿用原先以 IP 保存的打印偏好。

本方法不修改 LuCI 的 80／443 監聽、不替換 `/www/index.html`，也不增加反向代理或重啟打印容器。原來的 IP 網址與 LuCI 頁面繼續使用。專案製作及核驗說明見 [README](../README.md)。

## 先檢查與備份

以下主機命令在 OpenWrt 以 root 執行；範例假設既有 uhttpd 區段為 `main`，dnsmasq 使用 OpenWrt 原生設定。

```sh
uci show uhttpd.main
uci changes uhttpd
uci changes dhcp
```

先處理原有未提交的修改。若 `redirect_https` 為 `1`，uhttpd 會在自訂處理規則之前轉到 HTTPS，本教程的 HTTP 入口不能生效；請保留該設定，另行安排入口。不要為打印服務停用既有管理頁面的 HTTPS 轉向。

確認 `http://192.168.1.1:8080/` 已能開啟，並記錄 `http://192.168.1.1/`、`https://192.168.1.1/` 的原有結果。備份保存在路由器私人目錄：

```sh
backup_dir="/root/fpc-domain-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -m 700 "$backup_dir"
cp -p /etc/config/uhttpd /etc/config/dhcp "$backup_dir/"
[ ! -f /etc/sysupgrade.conf ] || cp -p /etc/sysupgrade.conf "$backup_dir/"
```

下列 `fpc_print_domain` 區段、JSON 路徑如已存在，先核對用途，不直接覆寫。

## 建立並單獨驗證重定向

將以下內容存為 `/etc/fpc-print-redirect.json`，權限設為 `644`：

```json
{
  "request": [
    [
      "if",
      [
        "and",
        ["eq", "HTTP_HOST", ["print.home.arpa", "print.home.arpa:80", "print.home.arpa.", "print.home.arpa.:80"]],
        ["eq", "REQUEST_METHOD", ["GET", "HEAD"]],
        ["not", ["eq", "HTTPS", "on"]]
      ],
      [
        ["add-header", "Cache-Control", "no-store"],
        ["redirect", "http://192.168.1.1:8080/", 302, "Found"]
      ]
    ]
  ]
}
```

注意 `request` 是「命令陣列的陣列」：必須保留 `"request": [["if", ...]]` 的兩層括號，直接寫成 `"request": ["if", ...]` 不會執行規則，即使 JSON 語法有效。只匹配完整名稱及 HTTP 的 GET／HEAD，目標固定，並要求不快取重定向。其他 Host、HTTPS 與其他方法繼續原有處理；名稱下的其他路徑也回到打印中心首頁。

先在電腦終端透過 SSH 啟動一個只監聽路由器 loopback 的臨時 uhttpd；須確認兩端的 `18081` 未被占用：

```sh
ssh -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:18081:127.0.0.1:18081 root@192.168.1.1 \
  '/usr/sbin/uhttpd -f -H /etc/fpc-print-redirect.json -p 127.0.0.1:18081 -h /www'
```

在電腦另一個終端驗證，不跟隨重定向：

```sh
curl --noproxy '*' -sS -D - -o /dev/null \
  -H 'Host: print.home.arpa' http://127.0.0.1:18081/
curl --noproxy '*' -sS -I \
  -H 'Host: print.home.arpa:80' http://127.0.0.1:18081/
curl --noproxy '*' -sS -D - -o /dev/null \
  -H 'Host: 192.168.1.1' http://127.0.0.1:18081/
curl --noproxy '*' -sS -D - -o /dev/null \
  -H 'Host: print.home.arpa.example' http://127.0.0.1:18081/
```

前兩項應為 `302` 且 `Location: http://192.168.1.1:8080/`；後兩項應顯示原來的 LuCI 入口，不轉到打印中心。測試完成後在第一個終端按 Ctrl+C，結束臨時服務和 SSH 轉送。`-H` 在部分版本的說明文字中未列出；以實際啟動及測試結果判斷支援，失敗時不要繼續修改正式服務。

## 保存 DNS 與正式入口

先確認沒有同名 DNS 記錄；只新增此完整名稱，不覆蓋整個網域。`hostrecord` 會生成 dnsmasq 的 `host-record`：

```sh
uci set dhcp.fpc_print_domain='hostrecord'
uci set dhcp.fpc_print_domain.name='print.home.arpa'
uci set dhcp.fpc_print_domain.ip='192.168.1.1'
uci commit dhcp
/etc/init.d/dnsmasq restart
```

保留原有 `json_script`，以 `add_list` 追加；首次設定只執行一次，避免重複項目：

```sh
uci add_list uhttpd.main.json_script='/etc/fpc-print-redirect.json'
uci commit uhttpd
/etc/init.d/uhttpd restart
```

uhttpd 重啟期間管理頁面短暫不可用，但埠號與監聽位址不變。JSON 改動後也須重新啟動 uhttpd 才載入；不需要重新啟動路由器或 Docker。

將自訂檔案加入保留清單，供選擇保留設定的系統升級使用：

```sh
touch /etc/sysupgrade.conf
grep -qxF '/etc/fpc-print-redirect.json' /etc/sysupgrade.conf || \
  printf '%s\n' '/etc/fpc-print-redirect.json' >> /etc/sysupgrade.conf
```

UCI 設定與 `/etc` 下的檔案可跨一般重新啟動保存；不保留設定的韌體升級／重設需重新配置。升級後核對 JSON 和 uhttpd 支援，再測試入口。

在使用家庭 DNS 的電腦上檢查：

```sh
nslookup -type=A print.home.arpa 192.168.1.1
nslookup -type=AAAA print.home.arpa 192.168.1.1
curl --noproxy '*' -sS -D - -o /dev/null http://print.home.arpa/
```

A 記錄應只有預期 LAN IP；未配置 IPv6 時，AAAA 不應回傳其他位址。再確認原來的 LuCI HTTP／HTTPS 入口、打印中心及上傳預覽均正常。測試網址與預覽不需要送出實體打印。

## 使用中文名稱

若選擇 `打印.com`，其 ASCII／Punycode 名稱為 `xn--wlr595a.com`。將 DNS 的 `name` 改為該 ASCII 名稱，並把 JSON 的 Host 清單換成：

```json
["xn--wlr595a.com", "xn--wlr595a.com:80", "xn--wlr595a.com.", "xn--wlr595a.com.:80"]
```

瀏覽器輸入 `http://打印.com/`，通常會以 Punycode 送出 Host。這份規則採完整、區分大小寫的比對；瀏覽器通常會把主機名稱轉為小寫。調整後先重做臨時服務測試，再重啟正式 uhttpd。只選擇一個名稱時，修改既有的專用 DNS 區段即可，不須建立重複區段。

此名稱只在使用家庭 DNS 時有效。手機行動網路、VPN、瀏覽器安全 DNS／DoH 或私人 DNS 可能繞過路由器；公開 `.com` 在家外可能指向他人的網站。`home.arpa` 是家庭網路保留名稱，可避免占用公開名稱。明確輸入 `http://`；本設定不提供中文網域 HTTPS 憑證，瀏覽器強制 HTTPS 時不能保證直接進入。不要略過憑證警告來測試。

## 代理軟體回傳假 IP

若直接詢問路由器得到正確 LAN IP，但電腦一般查詢得到 `198.18.x.x` 等假 IP，或瀏覽器仍開不到入口，檢查 Mihomo／Clash 類軟體的 DNS 與代理規則。`curl --noproxy '*'` 只能略過顯式 HTTP 代理，不能保證繞過 TUN 或 DNS 攔截。

Mihomo 使用預設 `blacklist` 模式時，可將所選的完整名稱追加至 `dns.fake-ip-filter`，在 `dns.nameserver-policy` 指向路由器 DNS，並把 `DOMAIN,print.home.arpa,DIRECT` 放在會搶先匹配的代理規則之前。使用中文網址時，這三處均填 `xn--wlr595a.com`。只排除 fake IP 而仍使用外部 DNS，無法取得家庭記錄；只設 DIRECT 也不等於使用路由器 DNS。

保留其他既有項目，不把整份 DNS 或代理設定換掉。若設定了 `direct-nameserver`，還要確認直連解析遵循 `nameserver-policy`；`fake-ip-filter-mode` 為 `whitelist` 或 `rule` 時，依該模式調整，不能直接套用 blacklist 清單。修改後重新載入設定、清除該名稱快取，再核對 DNS、302 和最終打印頁面。這些是排查方法，須在實際裝置驗證結果。設定語法見 [Mihomo DNS 文件](https://wiki.metacubex.one/en/config/dns/)及[規則文件](https://wiki.metacubex.one/en/config/rules/)。

## 只撤銷本次設定

先確認下列區段與檔案仍是本教程建立的項目，再移除。不要用舊的完整備份覆蓋之後新增的路由器設定。

```sh
uci -q del_list uhttpd.main.json_script='/etc/fpc-print-redirect.json'
uci -q delete dhcp.fpc_print_domain
uci commit uhttpd
uci commit dhcp
/etc/init.d/uhttpd restart
/etc/init.d/dnsmasq restart
rm -f /etc/fpc-print-redirect.json
sed -i '\|^/etc/fpc-print-redirect\.json$|d' /etc/sysupgrade.conf
```

清除裝置／瀏覽器的 DNS 快取後再測試。原有 IP 加 `:8080` 的打印網址不受影響。

實作依據：[OpenWrt uhttpd 啟動腳本](https://github.com/openwrt/openwrt/blob/main/package/network/services/uhttpd/files/uhttpd.init)、[uhttpd JSON 處理](https://github.com/openwrt/uhttpd/blob/master/handler.c)、[libubox 條件語法](https://github.com/openwrt/libubox/blob/master/json_script.c)、[OpenWrt dnsmasq hostrecord](https://github.com/openwrt/openwrt/blob/main/package/network/services/dnsmasq/files/dnsmasq.init)、[RFC 8375](https://www.rfc-editor.org/rfc/rfc8375.html)。
