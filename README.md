# wota艺封面制作

面向 wota艺 / 打艺 / 荧光棒表演的保色残影和封面工具，可作为 Codex skill 或独立 Python 脚本使用。适配单色、多色、渐变色或近白色光轨；开放弧线、波浪、螺旋、交叉、环形等形态均可，不预设颜色或几何形状。

## 功能

- 裁剪视频，以有限历史窗口生成等亮度残影，保留同步原声。
- 自动沿用实际帧率，适配常见 24、25、23.976 fps。
- 生成带时间/帧号的联系表，由看图模型按曲线流畅感、美感、人物姿态和构图挑选候选；也支持使用者自行选择。
- 以整 RGB 像素选择最亮历史帧，避免逐通道最大值造成假色。
- 基于亮度和时间变化估计光轨遮罩；支持手动光轨遮罩和前景保护遮罩。
- 默认保色提亮光芯，在遮罩外压暗背景；白化仅为显式选项。
- 导出封面、源帧、未调色光轨、遮罩、对比图和参数。

## 安装

需要 Python 3.10+ 和 Git。FFmpeg 优先使用系统安装，没有时通过 imageio-ffmpeg 提供。

Windows PowerShell：

```powershell
git clone https://github.com/ZhSM27/wota-cover.git "$env:USERPROFILE/.codex/skills/wota-cover"
python -m pip install -r "$env:USERPROFILE/.codex/skills/wota-cover/requirements.txt"
```

macOS / Linux：

```sh
git clone https://github.com/ZhSM27/wota-cover.git "${CODEX_HOME:-$HOME/.codex}/skills/wota-cover"
python3 -m pip install -r "${CODEX_HOME:-$HOME/.codex}/skills/wota-cover/requirements.txt"
```

目录已存在时先检查内容，或在对应 Git 仓库内执行 `git pull`。在新会话中用 `$wota-cover` 提供视频路径、时间范围和目标效果。

## 命令行

在仓库根目录运行。下列时间仅为任意示例，按自己的素材替换：

```sh
python scripts/wota_cover.py render --input source.mp4 --start 10 --end 12 --trail-seconds 0.4 --output clip.mp4
python scripts/wota_cover.py contact --input clip.mp4 --step 1 --output contact.jpg
python scripts/wota_cover.py cover --input source.mp4 --time 11 --trail-seconds 0.4 --output cover.png --comparison compare.jpg
```

联系表时间相对片段；加上裁剪起点后，才是 `cover --time` 使用的源视频绝对时间。

选帧时让看图模型实际查看联系表，粗选 2–4 帧并查看原尺寸与相邻帧，再给出选择理由。重点是曲线流畅、走势舒展、疏密有层次、人物动作与视觉重心清楚，不要求闭合成圆；开放端点和自然出画也可有美感。脚本只产出候选素材，命令行不会自行调用模型或替你做审美判断。

`cover-source.png` 为源视频对应帧，`cover-ungraded.png` 为未调色光轨，`cover-light-mask.png` 为实际使用的光轨遮罩，`cover.json` 保存参数。对比图依次显示源帧、未调色光轨和成品。视频另存 JSON。

| 参数 | 含义 | 默认 |
| --- | --- | --- |
| `--fps` | 显式覆盖采样/输出帧率 | 沿用源帧率 |
| `--trail-seconds` | 最老残影与当前帧的时间差 | 约 0.4 秒 |
| `--frames` | 含当前帧的窗口帧数，与秒数选项互斥 | 按秒数换算 |
| `--core` | 光芯增强幅度 | 0.25 |
| `--core-mode` | `preserve` 保色，`white` 显式白化 | preserve |
| `--shadow` / `--highlight` | 暗部 / 高光中性曝光倍率 | 1 / 1 |
| `--background` | 光轨和保护遮罩外的压暗强度 | 0.15 |
| `--light-mask` | 覆盖自动估计的灰度光轨遮罩 | 自动估计 |
| `--protect-mask` | 保护人物等前景免受背景压暗 | 无 |
| `--light-threshold` | 自动遮罩的亮度阈值 | 0.55 |
| `--motion-threshold` | 自动遮罩的时间变化阈值 | 0.08 |
| `--core-opacity` | 光芯与局部色光底层的混合比例 | 1 |
| `--overwrite` | 允许覆盖已有输出 | 关闭 |

约 0.4 秒在 24 fps 对应 11 帧（约 0.417 秒），25 fps 对应 11 帧（0.4 秒）。窗口 N 帧的跨度是 `(N−1)/fps`。增加一帧应增加 N，不改变帧率。

## 多颜色调色与遮罩

默认使用同样的 RGB 倍率增强光芯，并在最先饱和的通道处限制增益，避免各通道独立剪切造成变色。饱和色光已经达到峰值时，无法同时保持颜色比例并继续提高峰值；此时可减少背景亮度，或由使用者选择 `--core-mode white`。白化会降低饱和度，不是默认步骤。

自动遮罩不检查固定色相，而是结合最大通道亮度与时间变化；因此不会刻意把某一种颜色当作背景。它仍可能误选运动衣物、闪灯和镜头移动，漏掉静止光弧。应先查看遮罩，再用同源尺寸的灰度 PNG 修正：白选光轨、黑为背景、灰为羽化。人物可单独提供保护遮罩。

```sh
python scripts/wota_cover.py cover --input source.mp4 --time 11 --output masked-cover.png --light-mask light-mask.png --protect-mask person-mask.png --background 0.2
```

先看源帧、未调色光轨和遮罩，再分轮调整光芯、曝光和背景，实际查看后再继续。详见 [调色说明](references/tuning.md)。默认数值是保守起点，不代表所有素材的最优设置。

## 范围与限制

光轨来自真实历史帧，不补画或拼凑几何形状；不同颜色在同一像素竞争时只选一个源像素，不做物理光照重建。没有脚本内置的审美模型、自动选帧、抠像或镜头稳定功能。已饱和高光不能恢复缺失细节。

脚本按固定帧率采样；可变帧率输入会转换为检测到的平均帧率。需要保留原始逐帧时间戳时应另行处理。裁剪时长量化到整帧，输出保持原尺寸，参数记录实际帧数和时长。

## 检查

```sh
python -m unittest discover -s tests -v
```

测试只使用合成素材，覆盖多颜色保色、色相无关遮罩、人物保护、显式白化，以及 24/25/23.976 fps 的视频、音轨、封面和联系表流程。这些检查不能代替在复杂真实表演中审查遮罩。

## 许可

[MIT License](LICENSE)。仓库不含特定表演视频、人物图像或素材专属记录。处理素材的权利由使用者管理；FFmpeg 和依赖遵循各自许可证。
