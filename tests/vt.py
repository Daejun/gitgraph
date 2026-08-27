"""tiny VT100 screen emulator good enough for curses output: returns the final screen as text lines."""
import re, unicodedata
def cw(ch): return 2 if unicodedata.east_asian_width(ch) in "WF" else (0 if unicodedata.combining(ch) else 1)
class Screen:
    def __init__(self, rows, cols):
        self.rows, self.cols = rows, cols; self.buf=[[" "]*cols for _ in range(rows)]; self.y=self.x=0
    def feed(self, data):
        i=0; n=len(data)
        while i<n:
            ch=data[i]
            if ch=="\x1b":
                m=re.match(r"\x1b\[([0-9;?]*)([A-Za-z@])", data[i:])
                if m:
                    params, cmd = m.group(1), m.group(2); i+=m.end()
                    nums=[int(x) if x else 0 for x in params.lstrip("?").split(";")] if params else []
                    if cmd=="H" or cmd=="f": self.y=(nums[0]-1 if nums else 0); self.x=(nums[1]-1 if len(nums)>1 else 0)
                    elif cmd=="A": self.y=max(0,self.y-(nums[0] or 1 if nums else 1))
                    elif cmd=="B": self.y=min(self.rows-1,self.y+(nums[0] or 1 if nums else 1))
                    elif cmd=="C": self.x=min(self.cols-1,self.x+(nums[0] or 1 if nums else 1))
                    elif cmd=="D": self.x=max(0,self.x-(nums[0] or 1 if nums else 1))
                    elif cmd=="G": self.x=(nums[0]-1 if nums else 0)
                    elif cmd=="d": self.y=(nums[0]-1 if nums else 0)
                    elif cmd=="X":
                        for xx in range(self.x, min(self.cols, self.x+(nums[0] or 1 if nums else 1))): self.buf[self.y][xx]=" "
                    elif cmd=="P":
                        n_=(nums[0] or 1 if nums else 1); row=self.buf[self.y]; del row[self.x:self.x+n_]; row.extend([" "]*n_)
                    elif cmd=="@":
                        n_=(nums[0] or 1 if nums else 1); row=self.buf[self.y]; self.buf[self.y]=row[:self.x]+[" "]*n_+row[self.x:self.cols-n_]
                    elif cmd=="K":
                        for xx in range(self.x,self.cols): self.buf[self.y][xx]=" "
                    elif cmd=="J":
                        for yy in range(self.y,self.rows):
                            for xx in range(self.x if yy==self.y else 0,self.cols): self.buf[yy][xx]=" "
                    continue
                m=re.match(r"\x1b[()][A-Z0-9]|\x1b[=>78]|\x1b\][^\x07]*\x07", data[i:])
                if m: i+=m.end(); continue
                i+=1; continue
            if ch=="\r": self.x=0
            elif ch=="\n": self.y=min(self.rows-1,self.y+1)
            elif ch=="\b": self.x=max(0,self.x-1)
            elif ch=="\t": self.x=min(self.cols-1,(self.x//8+1)*8)
            elif ch>=" ":
                w=cw(ch)
                if self.x+w>self.cols: self.x=0; self.y=min(self.rows-1,self.y+1)
                if w: self.buf[self.y][self.x]=ch
                if w==2 and self.x+1<self.cols: self.buf[self.y][self.x+1]=""
                self.x+=w
            i+=1
    def text(self): return "\n".join("".join(r).rstrip() for r in self.buf)
