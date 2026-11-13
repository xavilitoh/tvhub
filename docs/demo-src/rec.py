import asyncio, io, os, time
from playwright.async_api import async_playwright
from PIL import Image
E=os.environ.get("CHROMIUM") or None   # None = the browser Playwright installed
FR=[]   # (image, seconds)
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path=E)
        ctx = await b.new_context(viewport={"width": 1280, "height": 760})
        await ctx.add_init_script("delete window.VideoDecoder; new MutationObserver(()=>{const t=document.getElementById('toast'); if(t) t.style.visibility=/WebCodecs/.test(t.textContent)?'hidden':'';}).observe(document,{subtree:true,childList:true,characterData:true,attributes:true});")
        pg = await ctx.new_page()
        await pg.goto("http://127.0.0.1:8099/")
        await pg.fill("#liUser", "demo"); await pg.fill("#liPass", "demo-password-123"); await pg.click("#liGo")
        await pg.wait_for_timeout(1500)
        async def rec(sec):
            end = time.time() + sec
            while time.time() < end:
                t0 = time.time()
                im = Image.open(io.BytesIO(await pg.screenshot(type="jpeg", quality=88))).convert("RGB")
                await asyncio.sleep(max(0, .12 - (time.time() - t0)))
                FR.append((im, time.time() - t0))
        await rec(2.0)
        await pg.click(".wtile >> nth=0"); await rec(1.2)
        await pg.click("text=Ver en vivo"); await rec(2.2)
        await pg.click(":is(button,div,a):visible:has-text(\"YouTube\") >> nth=-1"); await rec(2.0)
        for _ in range(3): await pg.click("button:visible:has-text(\"Vol +\")"); await rec(.5)
        await pg.click("button:visible:has-text(\"Mute\")"); await rec(.8)
        await pg.click("#brand"); await rec(2.0)
        await pg.click("text=Buscar en la red"); await rec(.8)
        await pg.click("#scanGo"); await rec(4.2)
        await b.close()
asyncio.run(main())
W=860; H=round(760*W/1280)
frames=[im.resize((W,H), Image.LANCZOS) for im,_ in FR]
dur=[int(d*1000) for _,d in FR]
print(len(frames), sum(dur)/1000)
q=[f.quantize(256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE) for f in frames]
q[0].save("demo.gif", save_all=True, append_images=q[1:], duration=dur, loop=0, optimize=True, disposal=2)
import os; print(os.path.getsize("demo.gif")/1e6,"MB")
