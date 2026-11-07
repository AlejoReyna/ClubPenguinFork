   // Portrait page: the camera shows a narrow strip of the 760x480 stage that follows our penguin,
   // so the bottom dock would slide off screen with the room. Keep the dock inside that strip with
   // the chat input in the middle, shrunk only as much as needed for every button to fit, and move
   // the emote, action and safe-chat menus with it (their close buttons sit over the dock buttons).
   // The page answers "left,right" (visible stage x range); anything else (the whole stage is
   // visible: landscape, desktop, "show the whole game") restores the original layout.
   // It only moves interface clips: it never reads chat, packets or other players.
   function updateMobileDock()
   {
      var hud = this._interface.interface_mc;
      var dock = hud.dock_mc;
      if(dock == undefined || dock.chat_mc == undefined || !flash.external.ExternalInterface.available)
      {
         return;
      }
      var layout = dock.__glaciarLayout;
      if(layout == undefined)
      {
         // Measured once, at the original layout, while the dock is shown and the buddy notice parked.
         if(!dock._visible || dock.buddy_online_mc._visible)
         {
            return;
         }
         var bar = dock.getBounds(hud);
         var chat = dock.chat_mc.getBounds(hud);
         if(!(bar.xMax - bar.xMin > 200) || !(chat.xMax > chat.xMin))
         {
            return;
         }
         layout = {left:bar.xMin,right:bar.xMax,bottom:bar.yMax,chat:(chat.xMin + chat.xMax) / 2,clips:[]};
         var names = ["dock_mc","emote_menu_mc","action_menu_mc","safe_menu_mc"];
         var i = 0;
         while(i < names.length)
         {
            var clip = hud[names[i]];
            if(clip != undefined)
            {
               layout.clips.push({clip:clip,x:clip._x,y:clip._y,xscale:clip._xscale,yscale:clip._yscale});
            }
            i++;
         }
         dock.__glaciarLayout = layout;
      }
      var scale = 1;
      var shift = 0;
      var margin = 4;
      var from = {x:0,y:0};
      var view = String(flash.external.ExternalInterface.call("glaciarHudView")).split(",");
      if(view.length == 2)
      {
         var to = {x:Number(view[1]),y:0};
         from.x = Number(view[0]);
         hud.globalToLocal(from);
         hud.globalToLocal(to);
         if(to.x - from.x > 0)
         {
            // The chat stays centred, so the wider side of the bar decides how much it shrinks.
            scale = Math.max(0.2,Math.min(1,((to.x - from.x) / 2 - margin) / Math.max(layout.chat - layout.left,layout.right - layout.chat)));
            shift = (from.x + to.x) / 2 - layout.chat;
         }
      }
      var key = scale + "," + shift;
      var j = 0;
      var item;
      var list;
      while(j < layout.clips.length)
      {
         // Scale about the chat centre and the bottom of the bar, then slide to the strip's centre.
         item = layout.clips[j];
         item.left = layout.chat + shift + (item.x - layout.chat) * scale;
         // The safe-chat list opens left of its button, past the bar: slide it back on screen.
         list = item.clip.menu_mc;
         if(shift != 0 && list != undefined)
         {
            item.left = Math.max(item.left,from.x + margin - list.getBounds(item.clip).xMin * scale);
         }
         key += "," + item.left;
         j++;
      }
      if(key == layout.key)
      {
         return;
      }
      layout.key = key;
      j = 0;
      while(j < layout.clips.length)
      {
         item = layout.clips[j];
         item.clip._xscale = item.xscale * scale;
         item.clip._yscale = item.yscale * scale;
         item.clip._x = item.left;
         item.clip._y = layout.bottom + (item.y - layout.bottom) * scale;
         j++;
      }
   }
