---
title: Ikki JSON faylni taqqoslash
description: Ikki katta JSON yoki JSON Lines faylni taqqoslang va yozuv darajasidagi farqni (diff) oling — qator bo'yicha emas, kalit bo'yicha moslanadi — buyruqlar qatoridan yoki desktop dasturdan, GB miqyosida.
---

# Ikki JSON faylni taqqoslash (diff)

`diff` ikki JSON yoki JSON Lines faylni **yozuvma-yozuv** taqqoslaydi, yozuvlarni qator bo'yicha
emas, kalit bo'yicha moslaydi, shuning uchun shunchaki o'rni almashtirilgan yozuvlar o'zgarish
sifatida ko'rsatilmaydi.

```bash
jsonreshape diff old.json new.json --key id
```

- `--key id` yozuvlarni ularning `id` maydoni bo'yicha moslaydi (istalgan maydon yoki nuqtali yo'ldan foydalaning).
- Natija qo'shilgan, o'chirilgan va o'zgargan yozuvlarni, o'zgargan yozuvlar uchun esa qaysi
  maydonlar farqlanishini ro'yxatlaydi.
- `--exit-code` farqlar bo'lganda buyruqni noldan farqli chiqish kodi bilan tugatadi — CI quvurlari
  va skriptlarda qulay:

```bash
jsonreshape diff old.json new.json --key id --exit-code
```

## Desktop dasturda

Bitta faylni oching, so'ng ikkinchisini tanlash uchun **Taqqoslash…** dan foydalaning. Farqlar
yonma-yon ko'rsatiladi, qo'shilgan / o'chirilgan / o'zgargan bo'yicha guruhlanadi, o'zgargan
maydonlar ajratib ko'rsatiladi.

![Taqqoslash ko'rinishi](../img/compare.png){ loading=lazy }

## Aloqador

- Agar fayllardan biri yaroqsiz bo'lsa, avval [buzuq JSON'ni ta'mirlang](repair-broken-json.md).
- [JSON fayllarni bo'lish va birlashtirish](split-and-merge-json.md).
