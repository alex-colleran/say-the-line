# README screenshots

Taken from a demo index that holds only *His Girl Friday* (1940, public domain), so no other
titles from a real library can appear. [`take.py`](take.py) builds that index and takes the
shots (steps in its docstring). The files here are composed from its output with ffmpeg:

```bash
# phone.png: results, editor and finished GIF side by side
ffmpeg -i shots/390-1-results.png -i shots/390-2-editor.png -i shots/390-4-result.png -filter_complex \
  "[0]scale=390:-1,pad=iw+24:ih:12:0:color=0x0b0c0f[a];[1]scale=390:-1,pad=iw+24:ih:12:0:color=0x0b0c0f[b];[2]scale=390:-1,pad=iw+24:ih:12:0:color=0x0b0c0f[c];[a][b][c]hstack=3" phone.png
cp shots/1280-1-results.png desktop-search.png; cp shots/1280-2-editor.png desktop-editor.png
# clip.gif: the rendered GIF (demo-data/clips), made smaller
ffmpeg -i demo-data/clips/<name>.gif -filter_complex \
  "fps=10,scale=360:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=64[p];[b][p]paletteuse=dither=bayer:bayer_scale=4" clip.gif
```
