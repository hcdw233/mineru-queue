# MinerU 队列

独立调度程序。复制到服务器后，改好 `config.toml` 就可以跑。解析和进度网页分开启动。

任务来自 `mineru_tasks.csv`。每一行是一个 PDF，结果写到 `{output_root}/{output_rel_dir}/`。这个目录里同时放 `result.zip` 和解压出来的 markdown、json、assets。

## 安装

需要 Python 3.11 或更高版本，没有第三方依赖。

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
cp config.example.toml config.toml
```

## 配置

`[[services]]` 可以写任意多个 MinerU 服务。`concurrency.target` 是普遍并发，`concurrency.max` 是最高并发。某个服务如果写了自己的 `target` / `max`，就用它自己的值。A100 可以设成 `target = 2`、`max = 4`，到 4 之后不再接任务，也不再参加字节差计算，因此不会挡住 H100。

`output_formats` 里必须保留 `zip`。

## 远端文件回收（MinerU 4.0.5）

仓库的 [服务端安装脚本](scripts/install_mineru_gc.py) 针对官方 MinerU 4.0.5。先停止同一虚拟环境中的五个 API 服务，在集群上激活 MinerU 虚拟环境，然后执行：

```bash
python scripts/install_mineru_gc.py --check
python scripts/install_mineru_gc.py
```

脚本核对原文件 SHA-256、验证生成文件的 Python 语法，备份原始 `api_server.py` 后才替换。若版本或文件内容不同，脚本会拒绝安装。安装后照原来的 `mineru-kit api-server` 命令重启 8190–8194 五个端口；每个端口继续使用自己的 `--upload-dir`。服务端文件位置由当前虚拟环境自动查找，你的安装对应 `/public/home/lab5/project/pdfParse/mineru/.venv/lib/python3.13/site-packages/mineru/parser/api_server.py`。

随后在队列的 `config.toml` 中设置 `release_remote_jobs = true`，并重新启动队列。必须先更新并重启服务端，再打开此开关。服务端新增 `POST /v1/parse/jobs/{job_id}/release`；重复调用安全。成功任务的 ZIP 下载并解压后，客户端调用此接口，服务端立即删除该任务的上传源文件和输出文件。失败、取消和部分成功任务在服务端结束时立即回收；轮询超时的任务会由客户端取消并回收。文件内容按 SHA-256 去重，其他任务或文件记录仍引用同一 blob 时不会删除。

ZIP 下载失败时，队列在结果目录保留 `.mineru_job.json`，下次重跑会继续下载原任务，不会重新提交同一 PDF。释放请求失败时，队列在 `{output_root}/.mineru_pending_releases/` 留下记录，下次执行 `mineru-queue run --config config.toml` 会先重试释放。只有收到服务端释放确认才删除待重试记录。旧服务产生的既有 blob 不会被这次修改追溯清理。

## 启动

```bash
# 终端 1：开始解析。已经成功的任务会跳过，失败、超时、取消、部分成功和没跑过的会进入队列。
mineru-queue run --config config.toml

# 只重跑最新状态为 failed 的任务
mineru-queue run --config config.toml --only failed

# 终端 2：进度页面和 API，同一个端口
mineru-queue serve --config config.toml
```

浏览器打开 `http://服务器IP:8765/`。页面和 `/api` 在同一端口。Ctrl+C 一次会停止接收新任务，并等在途任务把进度写完。再按一次立即退出，没写完的任务下次启动会重跑。

进度页顶部突出显示当前任务表里最新状态为成功的总篇数，并显示任务总数、待完成篇数和完成率。同一篇重跑成功只占总完成量中的一篇。旁边显示最近 60 分钟实际成功完成的篇数，并对比前一个 60 分钟；同时显示本小时、上个完整小时、今天和最近 10 分钟的成功篇数。默认图表按小时展示当天完成量，也可切到某小时查看每 5 分钟的完成量。页面每分钟刷新。吞吐按成功完成事件统计，重跑成功会再次计入；仍在解析中的任务尚无完成记录，不计入吞吐。

## 调度

每个服务先补到自己的普遍并发。此阶段优先给在途任务更少的服务，数量相同再给当前 PDF 总字节更小的服务。

全部到达普遍并发之后，设最轻服务的在途字节为 `min`，最重服务为 `max`。队首任务的字节数 `<= max - min` 时，把它交给最轻的服务。这样每个还没到最高并发的服务都可能超过普遍并发。队首太大就等待，不插队，也不塞给别的服务。

已经到达最高并发的服务不接收任务，也不参与 `min` / `max`。只剩一个服务还没到最高并发时，没有第二份负载可以比较，它会继续接到自己的上限。

有任务完成后，如果某个服务掉回普遍并发以下，队首任务会直接补进去，不看字节差。因此大文件不会因为字节差一直不够而被卡住。

PDF 字节数第一次使用时 `stat`，并记在 `pdf_sizes.tsv`。文件不存在则记为 `failed`，不调用 MinerU。

## 进度文件

默认是 `{output_root}/progress.txt`。只追加，一行一次结束：

```text
user/10704yj01/1701162063479/10704_20103077009_LW	success	2026-09-26T08:48:01+08:00	2026-09-26T08:31:12+08:00
```

第四列是开始处理的时间，第三列是 zip 下载完成的时间。没有第四列的旧行仍可读取，但不参与按单篇耗时计算的平均速度。

| 状态 | 含义 | 网页归类 | 默认重跑 |
| --- | --- | --- | --- |
| `success` | MinerU `completed`，zip 已下载并解压 | 成功 | 跳过 |
| `failed` | MinerU `failed`，或 PDF 缺失、上传、协议、下载、解压失败 | 失败 | 会重跑 |
| `partial` | MinerU `partial`；启用回收后立即删除远端产物 | 其他 | 会重跑 |
| `canceled` | MinerU `canceled` | 其他 | 会重跑 |
| `timeout` | 轮询次数用尽；启用回收后取消远端任务 | 其他 | 会重跑 |

`queued` 和 `running` 是轮询中的状态，不写这个文件。同一个任务重跑会再追加一行。网页上的总数取每个任务的最后一行；按小时（每 5 分钟）和按天（每小时）的柱状图则按每一行的完成时间计数。

具体错误写在该任务目录的 `mineru_error.txt`，不写进总进度文件。

## MinerU 调用

对配置里的每个 `base_url`：

1. `POST /v1/uploads`
2. 按返回的地址 `PUT` PDF，再 `POST /v1/uploads/<id>/complete`
3. `POST /v1/parse/jobs`，格式为 markdown、middle_json、structured_content、zip
4. 轮询 `GET /v1/parse/jobs/<job_id>`，直到 `completed`、`partial`、`failed` 或 `canceled`
5. 下载 `files[].output_files.zip.file_id`，保存为 `result.zip` 并解压到同一目录
6. 启用回收后，客户端确认释放远端任务；失败或取消任务由服务端自动回收

API key 只加在与该服务同源的请求上。下载如果跳到别的地址，不会把 key 带过去。
