// 头像（后端见 users.set_avatar、server 的 /api/avatar）：账号页「头像」那一行换、恢复成文字头像。
// 原图在浏览器里先处理：超过 PICK_MAX 直接不收；居中裁成正方形、缩到 SIZE×SIZE，压成 WebP（浏览器压不了 WebP 就用 JPEG），
// 太大再降质量；服务器那边只收 PNG、JPEG、WebP、不超过 256 KB，两层都说清楚为什么不收。动图只取第一帧。
// 旧后端的 /api/me 不给 avatar，就没有这一行，各处照旧是文字头像
const { ref, computed } = Vue;
import { me, say, fail } from './util.js';

const PICK_MAX = 10 * 1024 * 1024;
const SIZE = 256;
const SEND_MAX = 256 * 1024;   // 同 users.AVATAR_MAX

const toBlob = (canvas, type, quality) => new Promise(r => canvas.toBlob(r, type, quality));

async function shrink(file) {
  if (!file.type.startsWith('image/')) throw new Error(`「${file.name}」不是图片`);
  if (file.size > PICK_MAX) throw new Error(`图片有 ${(file.size / 1048576).toFixed(1)} MB，最大 ${PICK_MAX / 1048576} MB`);
  let bmp;
  try { bmp = await createImageBitmap(file); } catch { throw new Error(`浏览器打不开「${file.name}」这张图片`); }
  const s = Math.min(bmp.width, bmp.height), canvas = document.createElement('canvas');
  canvas.width = canvas.height = SIZE;
  const ctx = canvas.getContext('2d');
  ctx.imageSmoothingQuality = 'high';
  ctx.drawImage(bmp, (bmp.width - s) / 2, (bmp.height - s) / 2, s, s, 0, 0, SIZE, SIZE);
  bmp.close?.();
  for (const q of [0.86, 0.7, 0.5]) {
    let blob = await toBlob(canvas, 'image/webp', q);
    if (blob?.type !== 'image/webp') blob = await toBlob(canvas, 'image/jpeg', q);
    if (blob && blob.size <= SEND_MAX) return blob;
  }
  throw new Error('压缩以后还是超过 256 KB');
}

export function useAvatar({ menu }) {
  // 取头像的地址：带版本，换了就是新地址；没有头像是空的，各处画名字头一个字
  const avatarSrc = computed(() => (me.avatar ? `/api/avatar?v=${me.avatar}` : ''));
  const avatarBusy = ref(false);
  let picker = null;
  function pickAvatar() {
    // 每次新建一个选文件的框：选同一张图也会触发 change
    picker = Object.assign(document.createElement('input'), { type: 'file', accept: 'image/*' });
    picker.addEventListener('change', () => picker.files[0] && uploadAvatar(picker.files[0]));
    picker.click();
  }
  async function send(method, body, type) {
    let res;
    try {
      res = await fetch('/api/avatar', { method, body, headers: type ? { 'Content-Type': type } : {} });
    } catch { throw new Error('无法连接服务器'); }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `${res.status} ${res.statusText}`);
    return data.avatar;
  }
  async function uploadAvatar(file) {
    if (avatarBusy.value) return;
    avatarBusy.value = true;
    try {
      const blob = await shrink(file);
      me.avatar = await send('PUT', blob, blob.type);
      say('头像换好了');
    } catch (e) { fail('换头像', e); }
    avatarBusy.value = false;
  }
  async function clearAvatar() {
    if (avatarBusy.value) return;
    avatarBusy.value = true;
    try {
      me.avatar = await send('DELETE');
      say('恢复成文字头像了');
    } catch (e) { fail('恢复文字头像', e); }
    avatarBusy.value = false;
  }
  // 账号页那一行：还没头像直接选图；有了先弹个小菜单，换一张或者恢复成文字头像
  function avatarRow(e) {
    if (avatarBusy.value) return;
    if (!me.avatar) return pickAvatar();
    e.stopPropagation();
    menu.open(e, [{ label: '换一张', run: () => { menu.close(); pickAvatar(); } },
                  { label: '恢复成文字头像', run: () => { menu.close(); clearAvatar(); } }]);
  }
  return { avatarSrc, avatarBusy, avatarRow };
}
