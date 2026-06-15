const express = require('express');
const ytdl = require('@distube/ytdl-core');
const app = express();

app.get('/download', async (req, res) => {
  const url = req.query.url;
  if (!url) return res.status(400).json({ error: 'missing url' });
  try {
    const info = await ytdl.getInfo(url);
    const format = ytdl.chooseFormat(info.formats, { quality: '137' }) 
                || ytdl.chooseFormat(info.formats, { qualityLabel: '720p' })
                || ytdl.chooseFormat(info.formats, { quality: 'highestvideo' });
    res.json({ url: format.url, title: info.videoDetails.title });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.listen(3000, () => console.log('running'));
