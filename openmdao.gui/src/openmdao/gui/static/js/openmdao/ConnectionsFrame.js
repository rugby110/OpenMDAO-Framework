
var openmdao = (typeof openmdao === "undefined" || !openmdao ) ? {} : openmdao ;

openmdao.ConnectionsFrame = function(model,pathname,src_comp,dst_comp) {
    var id = ('ConnectionsFrame-'+pathname).replace(/\./g,'-');
    openmdao.ConnectionsFrame.prototype.init.call(this, id,
        'Connections: '+openmdao.Util.getName(pathname));

    /***********************************************************************
     *  private
     ***********************************************************************/

    // initialize private variables
    var self = this,
        // component selectors
        componentsHTML = '<div style="width:100%;background:grey"><table>'
                       +        '<tr><td>Source Component:</td>'
                       +            '<td>Target Component:</td>'
                       +        '</tr>'
                       +        '<tr><td><input id="src_list" /></td>'
                       +            '<td><input id="dst_list" /></td>'
                       +        '</tr>'
                       + '</table></div>',
        componentsDiv = jQuery(componentsHTML)
            .appendTo(self.elm),
        src_cmp_selector = componentsDiv.find('#src_list'),
        dst_cmp_selector = componentsDiv.find('#dst_list'),
        // dataflow diagram
        connectionsCSS = 'background:grey; position:static; width:100%;overflow-x:hidden;overflow-y:auto',
        connectionsDiv = jQuery('<div style="'+connectionsCSS+'">')
            .appendTo(self.elm),
        dataflowID  = id + '-dataflow',
        dataflowDiv = jQuery('<div id='+dataflowID+' style="'+connectionsCSS+'">')
            .appendTo(connectionsDiv),
        dataflow = new draw2d.Workflow(dataflowID),
        // variable selectors and connect button
        variablesHTML = '<div style="'+connectionsCSS+'"><table>'
                      +        '<tr><td>Source Variable:</td><td>Target Variable:</td></tr>'
                      +        '<tr><td><input  id="src_list" /></td>'
                      +        '    <td><input  id="dst_list" /></td>'
                      +        '    <td><button id="connect" class="button">Connect</button></td>'
                      +        '</tr>'
                      + '</table></div>',
        variablesDiv = jQuery(variablesHTML)
            .appendTo(self.elm),
        src_var_selector = variablesDiv.find('#src_list'),
        dst_var_selector = variablesDiv.find('#dst_list'),
        connect_button = variablesDiv.find('#connect')
                        .click(function() {
                            var src = src_var_selector.val();
                            var dst = dst_var_selector.val();
                            model.issueCommand(self.pathname+'.connect("'+src+'","'+dst+'")');
                        }),
       showAllVariables = false;  // only show connected variables by default

    self.pathname = null;

    // plain grey background
    dataflow.setBackgroundImage(null);
    dataflowDiv.css({'background-color':'grey','position':'absolute','width':'100%'});

    // create context menu for toggling the showAllVariables option
    dataflow.getContextMenu=function(){
        var menu=new draw2d.Menu();
        if (showAllVariables) {
            menu.appendMenuItem(new draw2d.MenuItem("Show Connections Only",null,
                function(){
                    showAllVariables = false;
                    self.update();
                })
            );
        }
        else {
            menu.appendMenuItem(new draw2d.MenuItem("Show All Variables",null,
                function(){
                    showAllVariables = true;
                    self.update();
                })
            );
        }
        return menu;
    };


    function bindEnterKey(selector) {
        selector.autocomplete({
           select: function(event, ui) {
               selector.value = ui.item.value;
               ent = jQuery.Event('keypress.enterkey');
               ent.target = selector;
               ent.which = 13;
               selector.trigger(ent);
           },
           delay: 0,
           minLength: 0
        });
        selector.bind('keypress.enterkey', function(e) {
            if (e.which === 13) {
                selector.autocomplete('close');
                if (selector === src_cmp_selector) {
                    self.src_comp = e.target.value;
                }
                else {
                    self.dst_comp = e.target.value;
                }
                editConnections(self.pathname, self.src_comp, self.dst_comp);
            }
        });
    }

    bindEnterKey(src_cmp_selector);
    bindEnterKey(dst_cmp_selector);

    function loadData(data) {
        if (!data || !data.Dataflow || !data.Dataflow.components) {
            // don't have what we need, probably something got deleted
            self.close();
        }
        else {
            comp_list = jQuery.map(data.Dataflow.components,
                                   function(comp,idx){ return comp.name; });

            // update the output & input selectors with component list
            src_cmp_selector.html('');
            src_cmp_selector.autocomplete({source: comp_list});

            dst_cmp_selector.html('');
            dst_cmp_selector.autocomplete({source: comp_list});
        }

        if (self.src_comp) {
            src_cmp_selector.val(self.src_comp);
        }
        if (self.dst_comp) {
            dst_cmp_selector.val(self.dst_comp);
        }
        editConnections(self.pathname, self.src_comp, self.dst_comp);
    }

    function loadConnectionData(data) {
        if (!data || !data.outputs || !data.inputs) {
            // don't have what we need, probably something got deleted
            self.close();
        }
        else {
            dataflow.clear();
            figures = {};
            var i = 0,
                x = 15,
                y = 10,
                conn_list = jQuery.map(data.connections, function(n){return n;}),
                out_list  = jQuery.map(data.outputs, function(n){return self.src_comp+'.'+n.name;}),
                in_list   = jQuery.map(data.inputs, function(n){return self.dst_comp+'.'+n.name;});

            for (i = 0; i <conn_list.length; i++) {
                conn_list[i]=conn_list[i].split('.')[1];
            }
            jQuery.each(data.outputs, function(idx,outvar) {
                if (showAllVariables || conn_list.contains(outvar.name)) {
                    var src_name = self.src_comp+'.'+outvar.name,
                        src_path = self.pathname+'.'+src_name,
                        fig = new openmdao.VariableFigure(model,src_path,outvar,'output');
                    dataflow.addFigure(fig);
                    fig.setPosition(x,y);
                    figures[src_name] = fig;
                    y = y + fig.height + 10;
                }
            });

            x = 250;
            y = 10;
            jQuery.each(data.inputs, function(idx,invar) {
                if (showAllVariables || conn_list.contains(invar.name)) {
                    var dst_name = self.dst_comp+'.'+invar.name,
                        dst_path = self.pathname+'.'+dst_name,
                        fig = new openmdao.VariableFigure(model,dst_path,invar,'input');
                    dataflow.addFigure(fig);
                    fig.setPosition(x,y);
                    figures[dst_name] = fig;
                    y = y + fig.height + 10;
                }
            });
            
            dataflowDiv.height(y+'px');
            connectionsDiv.height(y+'px');
            connectionsDiv.show();
            variablesDiv.show();
            
            jQuery.each(data.connections,function(idx,conn) {
                // internal connections
                if ((conn[0].indexOf('.') > 0) && (conn[1].indexOf('.') > 0)) {
                    var src_name = conn[0],
                        dst_name = conn[1],
                        src_fig = figures[src_name],
                        dst_fig = figures[dst_name],
                        src_port = src_fig.getPort("output"),
                        dst_port = dst_fig.getPort("input");
                    c = new draw2d.Connection();
                    c.setSource(src_port);
                    c.setTarget(dst_port);
                    c.setTargetDecorator(new draw2d.ArrowConnectionDecorator());
                    c.setRouter(new draw2d.BezierConnectionRouter());
                    c.setCoronaWidth(10);
                    c.getContextMenu=function(){
                        var menu=new draw2d.Menu();
                        var oThis=this;
                        menu.appendMenuItem(new draw2d.MenuItem("Disconnect",null,function(){
                                var asm = self.pathname,
                                    cmd = asm + '.disconnect("'+src_name+'","'+dst_name+'")';
                                model.issueCommand(cmd);
                            })
                        );
                        return menu;
                    };
                    dataflow.addFigure(c);
                    src_port.setBackgroundColor(new draw2d.Color(0,0,0));
                    dst_port.setBackgroundColor(new draw2d.Color(0,0,0));
                }
                // TODO: handle connections to parent assembly vars (e.g. Vehicle.velocity)
                // TODO: show passthroughs somehow
            });

            // update the output & input selectors to current outputs & inputs
            src_var_selector.html('');
            src_var_selector.autocomplete({ source: out_list ,minLength:0});

            dst_var_selector.html('');
            dst_var_selector.autocomplete({ source: in_list ,minLength:0});
        }
    }

    /** edit connections between the source and destination objects in the assembly */
    function editConnections(pathname, src_comp, dst_comp) {
        if (src_comp && dst_comp) {
            self.pathname = pathname;
            self.src_comp = src_comp;
            self.dst_comp = dst_comp;

            model.getConnections(pathname, src_comp, dst_comp, loadConnectionData,
                function(jqXHR, textStatus, errorThrown) {
                    debug.error(jqXHR,textStatus,errorThrown);
                    self.close();
                }
            );
        }
        else {
            connectionsDiv.hide();
            variablesDiv.hide();
        }
    }

    /** handle message about the assembly */
    function handleMessage(message) {
        if (message.length !== 2 || message[0] !== self.pathname) {
            debug.warn('Invalid component data for:',self.pathname,message);
            debug.warn('message length',message.length,'topic',message[0]);
        }
        else {
            loadData(message[1]);
        }
    }

    /***********************************************************************
     *  privileged
     ***********************************************************************/

    /** if there is an object loaded, update it from the model */
    this.update = function() {
        if (self.pathname && self.pathname.length>0) {
            self.editAssembly(self.pathname,self.src_comp,self.dst_comp);
        }
    };

    /** get the specified assembly from model */
    this.editAssembly = function(path, src_comp, dst_comp) {
        if (self.pathname !== path) {
           if (self.pathname !== null) {
                model.removeListener(self.pathname, handleMessage);
            }
            self.pathname = path;
            model.addListener(self.pathname, handleMessage);
        }

        self.src_comp = src_comp;
        self.dst_comp = dst_comp;
    
        model.getComponent(path, loadData,
            function(jqXHR, textStatus, errorThrown) {
                debug.warn('ConnectionsFrame.editAssembly() Error:',
                            jqXHR, textStatus, errorThrown);
                // assume component has been deleted, so close frame
                self.close();
            }
        );
    };

    this.destructor = function() {
        if (self.pathname && self.pathname.length>0) {
            model.removeListener(self.pathname, handleMessage);
        }
    };

    this.editAssembly(pathname, src_comp, dst_comp);
};

/** set prototype */
openmdao.ConnectionsFrame.prototype = new openmdao.BaseFrame();
openmdao.ConnectionsFrame.prototype.constructor = openmdao.ConnectionsFrame;
