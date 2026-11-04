   // Experimental protocol: %xt%puffletalk%-1%room%puffle%text%.
   // fields[0] is the internal server room (-1); fields[1] is Houdini's igloo external id.
   // The shell stores igloos as external id - 1000 (handleJoinPlayerIgloo), so mirror that.
   // The server must authorize the recipient and sanitize the text first.
   function handlePuffleChat(fields)
   {
      if(fields.length != 4 || Number(fields[0]) != this._shell.getCurrentServerRoomId() || Number(fields[1]) - 1000 != this._shell.getCurrentRoomId())
      {
         return;
      }
      var puffle = this.getPuffleByID(Number(fields[2]));
      if(puffle == undefined || puffle.clip == undefined || !puffle.clip._visible)
      {
         return;
      }
      var message = String(fields[3]);
      if(message.length < 1 || message.length > 48)
      {
         return;
      }
      var i = 0;
      while(i < message.length)
      {
         var code = message.charCodeAt(i);
         var c = message.charAt(i);
         if(code < 32 || code == 127 || c == "%" || c == "|" || c == "<" || c == ">" || c == "&")
         {
            return;
         }
         i++;
      }
      // Reuse interface.swf's own BalloonManager (balloons_mc): same DefaultBalloon clip,
      // Burbank Small Medium format, 128 px max width, 125-frame life and replace-on-new-message.
      // Keyed by "puffle<id>" so it never collides with a penguin id; createMC gets an explicit
      // depth and linkage so no penguin-only path (findPlayerDepth, buddy/mascot lookup) runs.
      var balloons = this._interface.balloons_mc;
      if(balloons == undefined || balloons.showTextBalloon == undefined)
      {
         return;
      }
      var key = "puffle" + puffle.id;
      var clip = puffle.clip;
      var place = function()
      {
         var point = {x:0,y:18};
         clip.localToGlobal(point);
         balloons.globalToLocal(point);
         var mc = balloons.getMC(key);
         if(mc != null)
         {
            mc._x = point.x;
            mc._y = point.y;
         }
      };
      if(!balloons.mcExistsByID(key))
      {
         balloons.createMC(key,balloons.getNextHighestDepth(),{_x:0,_y:0},{linkage:"DefaultBalloon"});
      }
      place();
      balloons.showTextBalloon(key,message);
      var follow = clip.createEmptyMovieClip("__puffleBalloonFollow",clip.getNextHighestDepth());
      follow.onEnterFrame = place;
   }
