   // Mobile page (containers/web/index.html): phones skip the start banner and open the login form,
   // or the saved-penguin cards, directly. The page decides through glaciarSkipStart(); without an
   // answer (desktop, no ExternalInterface) the original flow is untouched.
   function glaciarSkipStart()
   {
      return flash.external.ExternalInterface.available && String(flash.external.ExternalInterface.call("glaciarSkipStart")) == "true";
   }
   // The same choice as the banner's login button (StartScreen.chooseNextView).
   function glaciarFirstView()
   {
      if(com.clubpenguin.login.LocalData.getSavedPlayers().length > 0)
      {
         this.gotoPlayerSelection();
      }
      else
      {
         this.gotoNewPlayer();
      }
   }
   // Every frame, tell the page which login view is showing so it can frame it on a phone:
   // "<view>" ("newplayer", "newplayer-noback", "playerlogin", "worldselection", "moreservers"),
   // "playerselection,box,left,top,right,bottom" (the cards: their layout depends on how many),
   // "<view>,error,left,top,right,bottom" while the shell's error window covers it (stage
   // coordinates), "<view>,dialog,publiccomputer|savepassword" for the remember-password prompts
   // (their full-screen dimming layer makes measured bounds useless), or "" once the login is gone. It reports only what is on screen: never the typed name,
   // the password or the saved penguins.
   function setupGlaciarLoginReport()
   {
      var owner = this;
      if(this._mc.__glaciarLogin != undefined)
      {
         this._mc.__glaciarLogin.removeMovieClip();
      }
      var watcher = this._mc.createEmptyMovieClip("__glaciarLogin",this._mc.getNextHighestDepth());
      watcher.last = "";
      watcher.onEnterFrame = function()
      {
         var state = owner.glaciarLoginState();
         if(state != this.last)
         {
            this.last = state;
            fscommand("glaciarLogin",state);
         }
      };
      watcher.onUnload = function()
      {
         if(this.last != "")
         {
            fscommand("glaciarLogin","");
         }
      };
   }
   function glaciarLoginState()
   {
      if(!this.glaciarShown(this._mc))
      {
         return "";
      }
      var view;
      var i = 0;
      while(i < this._views.length)
      {
         if(this.glaciarShown(this._views[i]))
         {
            view = this._views[i];
            break;
         }
         i++;
      }
      if(view == undefined)
      {
         return "";
      }
      var id = String(view.__constructor__.LINKAGE_ID);
      var name = id.substr(id.lastIndexOf(".") + 1).toLowerCase();
      var error = this._shell.error_window;
      if(error != undefined && error.bg != undefined && this.glaciarShown(error))
      {
         return name + ",error," + this.glaciarRect(error,0,0,error.bg._width,error.bg._height);
      }
      var prompt = this._mc.prompt;
      if(prompt != undefined && this.glaciarShown(prompt))
      {
         return name + ",dialog," + (prompt.save_btn != undefined ? "savepassword" : "publiccomputer");
      }
      if(view.back_btn != undefined && !view.back_btn._visible)
      {
         return name + "-noback";
      }
      var cards = view._currentPlayerScreen;
      if(cards != undefined && this.glaciarShown(cards))
      {
         var area = cards.getBounds(cards);
         return name + ",box," + this.glaciarRect(cards,area.xMin,area.yMin,area.xMax,area.yMax);
      }
      return name;
   }
   function glaciarShown(clip)
   {
      while(clip != undefined)
      {
         if(!clip._visible)
         {
            return false;
         }
         clip = clip._parent;
      }
      return true;
   }
   function glaciarRect(clip, left, top, right, bottom)
   {
      var from = {x:left,y:top};
      var to = {x:right,y:bottom};
      clip.localToGlobal(from);
      clip.localToGlobal(to);
      return Math.round(from.x) + "," + Math.round(from.y) + "," + Math.round(to.x) + "," + Math.round(to.y);
   }
