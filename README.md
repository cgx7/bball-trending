# 🏀 篮球海外热榜

每天自动抓取 YouTube 上最近 24 小时播放量最高的篮球视频,输出榜单 + 可选下载原片。

## 为什么长这样

你的网络连不上 YouTube(实测 youtube.com / googleapis.com / tiktok.com 全部超时,本地无代理)。
所以采集和下载都放在 **GitHub Actions 的海外服务器**上跑,结果通过 GitHub 带回本地——
**GitHub 你是能连上的**,整条链路就通了。

另一个设计取舍:没有用 YouTube 官方 API。因为办 Google Cloud 账号本身就需要翻墙,
而且数据中心 IP 调 API 容易被判机器人。这里改用 yt-dlp,不需要任何 key。

---

## 一次性配置(约 10 分钟,只做一次)

### 第 1 步:注册 GitHub

打开 https://github.com/signup ,用邮箱注册。免费账号足够,Actions 额度用不完。

### 第 2 步:建一个仓库

1. 登录后点右上角 `+` → **New repository**
2. Repository name 填 `bball-trending`
3. 选 **Public**(公开仓库的 Actions 完全免费;私有仓库每月也只有 2000 分钟免费额度,但够用)
4. **不要**勾 Add a README file
5. 点 **Create repository**

### 第 3 步:把文件传上去

**方式 A:GitHub Desktop(推荐,不用敲命令)**

1. 下载安装 https://desktop.github.com
2. 登录你的账号 → `File` → `Clone repository` → 选你刚建的 `bball-trending` → 克隆到本地
3. 把这个文件夹里的**所有内容**复制进克隆下来的目录(包括 `.github` 这个隐藏文件夹)
4. 回到 GitHub Desktop,左下角写一句说明,点 **Commit to main** → 再点 **Push origin**

> ⚠️ `.github` 是隐藏文件夹,资源管理器里要先在「查看」里勾上「隐藏的项目」才看得到。
> 这个文件夹不能少,少了定时任务就不会跑。

**方式 B:命令行**

```bash
cd bball-trending
git init
git add .
git commit -m "初始化篮球热榜"
git branch -M main
git remote add origin https://github.com/<你的用户名>/bball-trending.git
git push -u origin main
```

### 第 4 步:开启 Actions 写权限(**这步漏了会失败**)

仓库页面 → **Settings** → 左侧 **Actions** → **General** → 拉到底部 **Workflow permissions**
→ 选 **Read and write permissions** → **Save**

不这么做的话,采集能跑但榜单提交不回仓库。

### 第 5 步:手动跑一次验证

仓库页面 → 顶部 **Actions** 标签 → 左侧选 **🏀 每日篮球热榜** → 右边 **Run workflow** → 绿色按钮

等 1~3 分钟,刷新页面。跑成功后仓库里会多出一个 `reports/` 文件夹,
里面是 `2026-09-20.md`(当天的榜单)和 `latest.json`。

**如果失败**,点进那次运行看红色步骤的日志,常见原因见文末「常见问题」。

---

## 日常使用

### 看今天的榜单

```bash
cd bball-trending
git pull
```

然后打开 `reports/` 里当天的 `.md` 文件。里面是 Top 5 详情 + Top 20 表格。

不想敲命令就直接在 GitHub 网页上看,点进 `reports/` 文件夹点开日期文件即可。

### 下载原片

1. 仓库页面 → **Actions** → 左侧选 **⬇️ 下载榜单视频** → **Run workflow**
2. 填参数:
   - `ranks`:`1 2 3`(要下第几名,空格分隔)
   - `max_height`:`720`(省体积)或 `1080`(高清)
3. 跑完后,在那次运行的页面底部 **Artifacts** 区域下载 `bball-videos.zip`

> ⚠️ 体积限制:GitHub 免费账号 Artifact 存储只有 500MB。
> **3 条 720p 是安全区**(约 150~300MB),5 条 1080p 会超。要更多就分几次下。
> 产物只保留 1 天,记得及时下载。

---

## 本地运行(需要先开代理)

如果你哪天开了代理,也可以在本地直接跑:

```bash
pip install -r requirements.txt

# 开代理后设置(端口换成你实际的)
export HTTPS_PROXY=http://127.0.0.1:7890
export HTTP_PROXY=http://127.0.0.1:7890

python fetch_top.py              # 抓榜单
python download.py --ranks 1 2 3 # 下载
```

不联网想验证脚本是否正常,可以跑自检(用内置样例数据,不发任何请求):

```bash
python fetch_top.py --selftest
```

---

## 自定义

### 改关键词

编辑 `fetch_top.py` 顶部的 `DEFAULT_KEYWORDS`。当前是:

```python
"basketball highlights", "nba highlights", "nba best plays", "streetball",
"basketball mixtape", "dunk compilation", "nba today", "basketball shorts"
```

也可以在命令行临时改:

```bash
python fetch_top.py --keywords "nba playoffs" "college basketball" --top 10
```

### 改时间和窗口

编辑 `.github/workflows/daily.yml` 里的 `cron`。**注意 GitHub 用的是 UTC 时间**:

| 你想要的时间(北京) | 填的 cron |
|---|---|
| 08:00 | `0 0 * * *` |
| 12:00 | `0 4 * * *` |
| 16:00 | `0 8 * * *` |
| 20:00 | `0 12 * * *` |

现在是 `13 0` 和 `13 8`,即北京 08:13 与 16:13 各跑一次。
**刻意避开整点**:GitHub 定时任务在整点高峰期会排队延迟十几分钟。

为什么卡在 16:13 再跑一次?NBA 是美国晚上打的,换算成 UTC 大约 23:00~06:00。
跑太早会漏掉当天所有比赛。

### 改时间窗口

```bash
python fetch_top.py --window 48   # 改成 48 小时内
```

---

## ⚖️ 版权红线(这段请认真看)

榜单里每条都带了**版权风险**标记。标「高」的是疑似官方版权方(如 NBA 官方频道)。

必须知道的事实:

- **NBA 在中国的独家数字版权在腾讯手里**,官方素材直接搬运是他们主动维权的对象
- 已有实际的下架、封号、索赔案例,不是理论风险
- 平台审核对 NBA 原片有特征识别,改速、加字幕、镜像翻转这类"小改"挡不住

**能长期跑的形态**(也是在算法上更容易起量的):

| 做法 | 说明 |
|---|---|
| 解说二创 | 用片段做素材,你的价值在观点和解说,不是画面本身 |
| 数据可视化 | 自己画数据图表讲比赛,几乎不涉及版权素材 |
| 深度分析 | 战术拆解、球员成长线,配自制示意图 |
| 混剪重构 | 多个来源重组 + 你的叙事逻辑,而不是原片重传 |

本工具只做**发现和下载**,帮你省掉"今天有什么值得做"的时间。
怎么加工、发不发,是你的判断。

---

## 常见问题

**Actions 跑失败了,报 `403` 或 `Permission denied`**
→ 第 4 步的写权限没开。Settings → Actions → General → Workflow permissions → Read and write。

**采集结果为空,榜单显示「本次没有采到数据」**
→ 大概率 YouTube 改版或临时风控。先手动 Run workflow 重试一次。
→ 持续为空的话,升级 yt-dlp:`pip install -U yt-dlp`(它更新很勤,YouTube 改版通常几天内就修)。

**定时任务不自动跑**
→ 确认 `.github/workflows/daily.yml` 在仓库里(少了 `.github` 隐藏文件夹是常见错误)。
→ 定时任务只在**默认分支**(main)上生效。
→ 仓库连续 60 天没有任何提交会被自动停用——但我们每天提交榜单,所以不会触发。

**下载的 Artifact 下载不了 / 失败了**
→ 超了 500MB 额度。改用 `720` 画质,或减少条数分几次下。

**能抓到 TikTok / Instagram 吗**
→ 不能。这两家没有公开的按播放量排行接口,抓取也不稳定。
   海外篮球视频的主战场就是 YouTube,覆盖度够用。
