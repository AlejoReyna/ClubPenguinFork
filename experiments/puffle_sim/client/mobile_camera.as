   // Passive view bridge for the portrait page: report only our own avatar's position on the
   // 760x480 stage so the page can keep it in view. It never reads chat, credentials, packets
   // or other players. Payload: "x,y,reset" (reset=1 on a new room or avatar), or "clear".
   function setupMobileCamera()
   {
      var owner = this;
      if(this._engine.__glaciarCamera != undefined)
      {
         this._engine.__glaciarCamera.removeMovieClip();
      }
      var camera = this._engine.createEmptyMovieClip("__glaciarCamera",this._engine.getNextHighestDepth());
      camera.onEnterFrame = function()
      {
         this.report();
         // After the report: the page has already moved the view for this frame (fscommand is synchronous).
         owner.updateMobileDock();
      };
      camera.report = function()
      {
         var clip = owner._engine.getPlayerMovieClip(owner._shell.getMyPlayerId());
         if(clip == undefined || !clip._visible || !clip.is_ready)
         {
            if(this.hadPlayer)
            {
               fscommand("glaciarCamera","clear");
               this.hadPlayer = false;
               this.previousClip = undefined;
            }
            return;
         }
         // Stage coordinates: the page maps 0..760 onto the rendered player width.
         var point = {x:0,y:0};
         clip.localToGlobal(point);
         var x = Math.round(point.x * 10) / 10;
         var y = Math.round(point.y * 10) / 10;
         if(isNaN(x) || isNaN(y))
         {
            return;
         }
         var room = owner._shell.getCurrentRoomId();
         var reset = this.previousClip != clip || this.previousRoom != room;
         if(reset || x != this.previousX || y != this.previousY)
         {
            fscommand("glaciarCamera",String(x) + "," + String(y) + "," + (reset ? "1" : "0"));
            this.previousClip = clip;
            this.previousRoom = room;
            this.previousX = x;
            this.previousY = y;
            this.hadPlayer = true;
         }
      };
   }
