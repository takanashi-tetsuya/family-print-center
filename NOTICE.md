# 授權與第三方檔案

本專案原創程式碼以 GNU Affero General Public License version 3 授權，
SPDX 識別碼為 `AGPL-3.0-only`，完整條款見 [LICENSE](LICENSE)。

網站使用的 Poppins 字體適用 SIL Open Font License 1.1，授權全文隨字體保存於
`print_center/static/fonts/OFL-Poppins.txt`，不適用本專案的 AGPL 授權。

Python、CUPS、LibreOffice 及其他依賴各自適用原授權。PyMuPDF 提供 AGPL v3／
Artifex 商業授權兩種方式；本專案使用其 AGPL 版本。相關軟體的完整條款以依賴套件
隨附的授權文件為準。

公開原始碼不包含 `deploy/fonts/cache/` 或使用者提供的字體檔。字體來源、下載雜湊
與安裝方式見 `deploy/fonts/sources.txt`。安裝程式保留下載包實際附帶的授權文件，
部分下載包未附獨立條款，仍須依原來源核對授權。
`deploy/fonts/private/` 內的字體須由部署者按各自授權取得及使用。

部署者可以設定 `SOURCE_CODE_URL`，讓家庭頁與管理頁連到所運行版本的完整原始碼。
修改後分享程式或提供網路服務時，請依 LICENSE 提供對應原始碼；不要在公開原始碼中
包含部署密碼、使用者文件或不可再分發的字體。
